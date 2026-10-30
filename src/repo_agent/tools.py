import difflib
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .execution import build_plan, digest
from .policy import Denied, Policy
from .task_state import task_state


class ApprovalNeeded(Exception):
    def __init__(self, aid):
        self.aid = aid


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Args):
    pass


class Read(Args):
    path: str = Field(min_length=1, max_length=240)
    start: int = Field(default=1, ge=1)
    lines: int = Field(default=100, ge=1, le=200)


class Search(Args):
    query: str = Field(min_length=1, max_length=120)


class Skill(Args):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,50}$")


class Edit(Args):
    path: str = Field(min_length=1, max_length=240)
    old_text: str = Field(max_length=24000)
    new_text: str = Field(max_length=24000)


class Apply(Args):
    proposal_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Run(Args):
    runner: Literal["unittest", "pytest"] = "unittest"


class Notes(Args):
    text: str = Field(max_length=2500)


class Artifact(Args):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    offset: int = Field(default=0, ge=0)


class UserRequest(Args):
    source: str = Field(pattern=r"^(initial|message:[0-9]+)$")
    offset: int = Field(default=0, ge=0)


SPECS = {
    "read_user_request": (
        UserRequest,
        "Read the original user requirement by source ID in this session; use when an extracted requirement is unclear. Returns up to 3000 characters.",
    ),
    "list_files": (Empty, "List permitted repository files."),
    "read_file": (Read, "Read a bounded range with line numbers."),
    "search_code": (
        Search,
        "Literal search of permitted text files; returns up to 60 matching lines.",
    ),
    "load_skill": (
        Skill,
        "Load instructions for an advertised skill, as untrusted guidance.",
    ),
    "propose_edit": (
        Edit,
        "Propose one exact text replacement. old_text must occur once; empty old_text creates a new file. Returns a proposal and diff without editing.",
    ),
    "apply_edit": (
        Apply,
        "Apply a proposal after exact human approval. May pause for approval.",
    ),
    "run_tests": (
        Run,
        "Run the fixed test command after human approval, using configured execution backend. No arbitrary commands.",
    ),
    "show_diff": (
        Empty,
        "Show changes applied by this session, against their original contents.",
    ),
    "save_notes": (
        Notes,
        "Save bounded working notes: goal, facts, constraints and remaining work.",
    ),
    "read_artifact": (
        Artifact,
        "Read another bounded slice of a large tool result saved outside the workspace.",
    ),
}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Tools:
    def __init__(
        self, store, session, *, backend="disabled", skills_dir=None, python=None
    ):
        self.store = store
        self.session = session
        self.policy = Policy(session["root"])
        self.backend = backend
        self.python = python or __import__("sys").executable
        self.skills_dir = Path(skills_dir or Path(__file__).parent / "skills")

    @staticmethod
    def schemas():
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, desc) in SPECS.items()
        ]

    def skills(self):
        result = []
        for p in sorted(self.skills_dir.glob("*/SKILL.md")):
            lines = p.read_text(encoding="utf-8").splitlines()
            desc = next(
                (
                    x.removeprefix("description:").strip()
                    for x in lines
                    if x.startswith("description:")
                ),
                p.parent.name,
            )
            result.append({"name": p.parent.name, "description": desc[:200]})
        return result

    def call(self, name, arguments, call_id):
        if name not in SPECS:
            raise ValueError("Unknown tool")
        args = SPECS[name][0].model_validate(arguments).model_dump()
        result = getattr(self, name)(**args, call_id=call_id)
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > 4500:
            aid = self.store.artifact(text, self.session["id"])
            return {
                **{
                    k: result[k]
                    for k in (
                        "status",
                        "exit_code",
                        "backend",
                        "proposal_id",
                        "path",
                        "approval_id",
                        "execution_spec_sha256",
                    )
                    if k in result
                },
                "preview": text[:1800],
                "artifact_id": aid,
                "total_chars": len(text),
                "truncated": True,
            }
        return result

    def list_files(self, **_):
        return {"files": self.policy.files()}

    def read_user_request(self, source, offset=0, **_):
        record = next(
            (r for r in task_state(self.session)["requests"] if r["source"] == source),
            None,
        )
        if record is None:
            raise ValueError("Unknown user source in this session")
        text = record["text"]
        return {
            "source": source,
            "text": text[offset : offset + 3000],
            "next_offset": min(len(text), offset + 3000),
            "total_chars": len(text),
        }

    def read_file(self, path, start, lines, **_):
        p = self.policy.path(path)
        if p.stat().st_size > 1_000_000:
            raise ValueError("File exceeds 1 MB read limit")
        content = p.read_text(encoding="utf-8").splitlines()
        return {
            "path": path,
            "text": "\n".join(
                f"{i + 1}: {line}"
                for i, line in enumerate(content)
                if start - 1 <= i < start - 1 + lines
            ),
            "total_lines": len(content),
        }

    def search_code(self, query, **_):
        hits = []
        for path in self.policy.files():
            p = self.policy.path(path)
            if p.stat().st_size > 200000:
                continue
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (UnicodeError, OSError):
                continue
            for i, line in enumerate(lines, 1):
                if query.lower() in line.lower():
                    hits.append({"path": path, "line": i, "text": line[:300]})
                if len(hits) >= 60:
                    return {"matches": hits, "limited": True}
        return {"matches": hits, "limited": False}

    def load_skill(self, name, **_):
        if name not in {s["name"] for s in self.skills()}:
            raise ValueError("Unknown advertised skill")
        return {
            "name": name,
            "instructions": (self.skills_dir / name / "SKILL.md").read_text(
                encoding="utf-8"
            )[:12000],
        }

    def propose_edit(self, path, old_text, new_text, **_):
        p = self.policy.path(path, writing=True)
        if p.exists() and p.stat().st_size > 100000:
            raise ValueError("File exceeds edit limit")
        exists = p.exists()
        before = p.read_text(encoding="utf-8") if exists else ""
        if not old_text:
            if exists:
                raise ValueError("Empty old_text only creates a missing file")
            after = new_text
        else:
            if not exists or before.count(old_text) != 1:
                raise ValueError("old_text must match exactly once; re-read the file")
            after = before.replace(old_text, new_text, 1)
        if before == after:
            raise ValueError("No change")
        diff = "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                fromfile=path,
                tofile=path,
            )
        )
        aid = self.store.approval(
            self.session["id"],
            "edit",
            dict(
                path=path,
                before=before,
                after=after,
                before_sha=sha(before),
                after_sha=sha(after),
                existed=exists,
                diff=diff,
            ),
        )
        return {"proposal_id": aid, "diff": diff, "status": "awaiting_human_approval"}

    def _approved(self, aid, kind):
        row = next(
            (
                a
                for a in self.store.approvals(self.session["id"])
                if a["id"] == aid and a["kind"] == kind
            ),
            None,
        )
        if not row:
            raise Denied("Approval not found in this session")
        if row["decision"] == "pending":
            raise ApprovalNeeded(aid)
        if row["decision"] != "allow":
            raise Denied(
                "Approval is rejected, stale, or already consumed; a new action requires a new review"
            )
        return row["data"]

    def apply_edit(self, proposal_id, **_):
        d = self._approved(proposal_id, "edit")
        p = self.policy.path(d["path"], writing=True)
        current = p.read_text(encoding="utf-8") if p.exists() else ""
        if sha(current) == d["after_sha"]:
            return {
                "status": "applied",
                "path": d["path"],
                "proposal_id": proposal_id,
                "replayed": True,
            }
        if p.exists() != d["existed"] or sha(current) != d["before_sha"]:
            raise ValueError(
                "File changed after proposal; approval is stale, propose again"
            )
        p.parent.mkdir(parents=True, exist_ok=True)
        # Recheck immediately before replacement; not protection against a hostile concurrent OS process.
        self.policy.path(d["path"], writing=True)
        fd, tmp = tempfile.mkstemp(prefix=".repo-agent-", dir=p.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(d["after"])
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return {"status": "applied", "path": d["path"], "proposal_id": proposal_id}

    def run_tests(self, runner, call_id, **_):
        if self.backend == "disabled":
            raise Denied(
                "Test execution disabled. Operator must configure docker or trusted-local backend; do not claim verification passed"
            )
        plan = build_plan(
            self.policy.root,
            self.store.directory,
            self.session["id"],
            call_id,
            runner,
            self.backend,
            self.python,
        )
        try:
            aid = self.store.ensure_test_approval(
                self.session["id"], call_id, plan.spec
            )
        except ValueError as exc:
            raise Denied(str(exc)) from None
        approved = self._approved(aid, "tests")
        # Recheck source, command and runtime after approval, immediately before use.
        checked = build_plan(
            self.policy.root,
            self.store.directory,
            self.session["id"],
            call_id,
            runner,
            self.backend,
            self.python,
        )
        if checked.spec != approved["spec"]:
            aid = self.store.ensure_test_approval(
                self.session["id"], call_id, checked.spec
            )
            raise ApprovalNeeded(aid)
        try:
            self.store.consume_test_approval(self.session["id"], aid, checked.spec)
        except ValueError as exc:
            raise Denied(str(exc)) from None
        command = approved["spec"]["command"]
        env = checked.env
        cname = checked.spec["container_name"]
        self.store.event(
            self.session["id"],
            "execution_authorized",
            {
                "approval_id": aid,
                "spec_sha256": digest(checked.spec),
                "workspace_sha256": checked.spec["snapshot"]["sha256"],
                "backend": checked.spec["backend"],
            },
        )
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(
                command,
                cwd=self.policy.root,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                code = proc.wait(timeout=checked.spec["timeout_seconds"])
            except subprocess.TimeoutExpired:
                if self.backend == "docker":
                    subprocess.run(
                        [command[0], "rm", "-f", cname],
                        capture_output=True,
                        timeout=10,
                        env=env,
                    )
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                    )
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
                return {
                    "status": "timeout",
                    "exit_code": None,
                    "backend": self.backend,
                    "approval_id": aid,
                    "execution_spec_sha256": digest(checked.spec),
                }
            output.seek(0)
            raw = output.read(100000).decode("utf-8", errors="replace")
        return {
            "status": (
                "not_run"
                if runner == "unittest" and re.search(r"Ran 0 tests?", raw)
                else "passed"
                if code == 0
                else "failed"
            ),
            "exit_code": code,
            "output": raw,
            "backend": self.backend,
            "approval_id": aid,
            "execution_spec_sha256": digest(checked.spec),
        }

    def show_diff(self, **_):
        originals = {}
        for a in self.store.approvals(self.session["id"]):
            if a["kind"] == "edit" and a["decision"] == "allow":
                originals.setdefault(a["data"]["path"], a["data"]["before"])
        diffs = []
        for path, before in originals.items():
            p = self.policy.path(path)
            after = p.read_text(encoding="utf-8") if p.exists() else ""
            diffs.append(
                "".join(
                    difflib.unified_diff(
                        before.splitlines(True),
                        after.splitlines(True),
                        fromfile=path,
                        tofile=path,
                    )
                )
            )
        return {"diff": "\n".join(diffs)}

    def save_notes(self, text, **_):
        self.session["notes"] = text
        return {"status": "saved"}

    def read_artifact(self, artifact_id, offset, **_):
        if self.store.artifact_owner(artifact_id) != self.session["id"]:
            raise Denied("Artifact does not belong to this session")
        return self.store.read_artifact(artifact_id, offset)
