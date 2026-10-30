import json
import threading
import time

from .context import assemble
from .execution import workspace_snapshot
from .provider import ProviderError
from .store import StoreConflict
from .task_state import protect_requirements
from .tools import ApprovalNeeded, Tools


class Engine:
    def __init__(
        self,
        store,
        provider,
        *,
        backend="disabled",
        context_budget=18000,
        max_tokens=100000,
        deadline=180,
        extractor=None,
        deliberator=None,
    ):
        self.store = store
        self.provider = provider
        self.backend = backend
        self.context_budget = context_budget
        self.max_tokens = max_tokens
        self.deadline = deadline
        self.extractor = extractor
        self.deliberator = deliberator
        self.locks = {}
        self.guard = threading.Lock()
        self.cancellations = {}

    def run(self, sid):
        with self.guard:
            root = self.store.get(sid)["root"]
            lock = self.locks.setdefault(root, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("Workspace already running another turn")
        with self.guard:
            self.cancellations[sid] = threading.Event()
        try:
            return self._run(sid)
        finally:
            lock.release()

    def cancel(self, sid):
        with self.guard:
            event = self.cancellations.get(sid)
            if event:
                event.set()
        return {"status": "stop_requested"}

    def follow_up(
        self,
        sid,
        text,
        max_steps=20,
        *,
        constraints=None,
        acceptance_criteria=None,
        origin="user",
    ):
        if not text.strip() or len(text) > 16000 or not 1 <= max_steps <= 40:
            raise ValueError("Invalid follow-up")
        with self.guard:
            root = self.store.get(sid)["root"]
            lock = self.locks.setdefault(root, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("Workspace is running")
        try:
            s = self.store.get(sid)
            if self.extractor:
                s["semantic_requirements_enabled"] = True
            if s["status"] in {"waiting_approval", "running"}:
                raise ValueError("Resolve current turn before adding a follow-up")
            if origin not in {"user", "approval_notice"}:
                raise ValueError("Invalid follow-up origin")
            message = {"role": "user", "content": text}
            if origin == "approval_notice":
                message["origin"] = origin
            s["messages"].append(message)
            if origin == "user":
                protect_requirements(s, constraints, acceptance_criteria)
            s["status"] = "ready"
            s["final"] = ""
            s["max_steps"] = s["steps"] + max_steps
            self.store.save(s)
            self.store.event(sid, "follow_up", {"message": text})
            return s
        finally:
            lock.release()

    def _run(self, sid):
        s = self.store.get(sid)
        if s["status"] in {
            "completed",
            "completed_unverified",
            "budget_exhausted",
            "stopped",
        }:
            return s
        tools = Tools(self.store, s, backend=self.backend)
        s["requirement_projection_enabled"] = self.extractor is not None
        s["status"] = "running"
        self.store.save(s)
        started = time.monotonic()
        try:
            # Upgrade old sessions from full, untrimmed user history before execution.
            try:
                if self.extractor:
                    s["semantic_requirements_enabled"] = True
                protect_requirements(s)
            except ValueError as exc:
                raise ProviderError(str(exc)) from exc
            if self.extractor:
                report = self.extractor.prepare(s, s["task_state"])
                s["requirements_status"] = report
                s["usage_tokens"] += report["usage_tokens"]
                self.store.event(sid, "requirements", report)
            self.store.save(s)
            if s.get("agent_mode") == "plan_execute" or s.get("learning_enabled"):
                if not self.deliberator:
                    raise ProviderError("Planning/learning model is not configured")
                try:
                    self.deliberator.prepare(s, self.store)
                except Exception as exc:
                    s["planning_error"] = type(exc).__name__
                    raise ProviderError(
                        "Plan preparation failed; retry explicitly"
                    ) from None
                self.store.save(s)
            while True:
                if self.cancellations.get(sid, threading.Event()).is_set():
                    s["status"] = "stopped"
                    s["final"] = (
                        "Stopped by operator; pending changes remain inspectable."
                    )
                    break
                # Complete persisted pending calls before requesting another model response.
                assistant = next(
                    (m for m in reversed(s["messages"]) if m["role"] == "assistant"), {}
                )
                calls = assistant.get("tool_calls", [])
                done = {
                    m.get("tool_call_id") for m in s["messages"] if m["role"] == "tool"
                }
                for call in calls:
                    if self.cancellations.get(sid, threading.Event()).is_set():
                        s["status"] = "stopped"
                        s["final"] = "Stopped before starting the next tool."
                        self.store.save(s)
                        return s
                    cid = call["id"]
                    if cid in done:
                        continue
                    if time.monotonic() - started > self.deadline:
                        s["status"] = "budget_exhausted"
                        s["final"] = "Turn deadline reached before next tool"
                        self.store.save(s)
                        return s
                    name = call["function"]["name"]
                    prior = self.store.call(sid, cid)
                    if prior and prior["status"] == "done":
                        result = json.loads(prior["result"])
                    elif prior and prior["status"] == "running" and name == "run_tests":
                        result = {
                            "error": "Execution interrupted; previous test process outcome is unknown. Re-check before retrying."
                        }
                    else:
                        self.store.put_call(sid, cid, "running")
                        self.store.event(sid, "tool_start", {"id": cid, "tool": name})
                        try:
                            arguments = json.loads(call["function"]["arguments"])
                            result = tools.call(name, arguments, cid)
                        except ApprovalNeeded as exc:
                            self.store.wait_for_approval(s, cid, name, exc.aid)
                            return s
                        except Exception as exc:
                            result = {
                                "error": str(exc)[:500],
                                "type": type(exc).__name__,
                            }
                    result.setdefault(
                        "_evidence_id",
                        "e" + str(1 + sum(m["role"] == "tool" for m in s["messages"])),
                    )
                    self.store.complete_call(s, cid, name, result)
                    if "error" in result:
                        signature = name + call["function"]["arguments"]
                        s["repeat_errors"] = (
                            s.get("repeat_errors", 0) + 1
                            if s.get("last_error_call") == signature
                            else 1
                        )
                        s["last_error_call"] = signature
                        if s["repeat_errors"] >= 3:
                            s["status"] = "stopped"
                            s["final"] = (
                                "Repeated identical tool failure; operator intervention required."
                            )
                            self.store.save(s)
                            return s
                    else:
                        s["repeat_errors"] = 0
                if (
                    s["steps"] >= s["max_steps"]
                    or s["usage_tokens"] >= self.max_tokens
                    or time.monotonic() - started > self.deadline
                ):
                    s["status"] = "budget_exhausted"
                    s["final"] = (
                        "Execution budget reached. Inspect changes before continuing."
                    )
                    break
                if self.cancellations.get(sid, threading.Event()).is_set():
                    continue
                context, cut = assemble(s, tools.skills(), self.context_budget)
                if cut:
                    s["compression_count"] += 1
                self.store.event(
                    sid,
                    "context",
                    {
                        "characters": len(json.dumps(context, ensure_ascii=False)),
                        "omitted_groups": cut,
                    },
                )
                step = s["steps"] + 1
                self.store.event(sid, "model_start", {"step": step})
                if hasattr(self.provider, "complete_stream"):
                    pending = []
                    last_emit = time.monotonic()

                    def flush_delta(piece="", force=False):
                        nonlocal last_emit
                        pending.append(piece)
                        if (
                            force
                            or sum(map(len, pending)) >= 80
                            or time.monotonic() - last_emit > 0.12
                        ):
                            chunk = "".join(pending)
                            pending.clear()
                            if chunk:
                                self.store.event(
                                    sid, "model_delta", {"step": step, "text": chunk}
                                )
                            last_emit = time.monotonic()

                    try:
                        msg, usage = self.provider.complete_stream(
                            context, tools.schemas(), flush_delta
                        )
                    finally:
                        flush_delta(force=True)
                else:
                    msg, usage = self.provider.complete(context, tools.schemas())
                self.store.event(sid, "model_end", {"step": step})
                s["steps"] += 1
                s["usage_tokens"] += int(usage.get("total_tokens", 0) or 0)
                tc = msg.get("tool_calls") or []
                if len(tc) > 8:
                    raise ProviderError("Provider exceeded eight tools per step")
                known = {
                    c["id"] for m in s["messages"] for c in m.get("tool_calls", [])
                }
                validated = []
                for c in tc:
                    if (
                        not isinstance(c.get("id"), str)
                        or not c["id"]
                        or len(c["id"]) > 255
                        or c["id"] in known
                    ):
                        raise ProviderError("Duplicate or invalid tool call ID")
                    f = c.get("function", {})
                    if (
                        not isinstance(f.get("name"), str)
                        or not isinstance(f.get("arguments"), str)
                        or len(f["arguments"]) > 60000
                    ):
                        raise ProviderError("Malformed tool call")
                    known.add(c["id"])
                    validated.append({"id": c["id"], "type": "function", "function": f})
                assistant = {
                    "role": "assistant",
                    "content": str(msg.get("content") or "")[:16000],
                }
                if validated:
                    assistant["tool_calls"] = validated
                s["messages"].append(assistant)
                self.store.save(s)
                if not tc:
                    if not assistant["content"].strip():
                        raise ProviderError(
                            "Empty model response; session retained for retry"
                        )
                    if s.get("verification") == "passed" and s.get(
                        "verification_snapshot"
                    ):
                        try:
                            unchanged = (
                                workspace_snapshot(s["root"], self.store.directory)[
                                    "sha256"
                                ]
                                == s["verification_snapshot"]
                            )
                        except Exception:
                            unchanged = False
                        if not unchanged:
                            s["verification"] = "stale"
                    s["status"] = (
                        "completed"
                        if s["revision"] == 0
                        or (
                            s["verification"] == "passed"
                            and s["test_revision"] == s["revision"]
                        )
                        else "completed_unverified"
                    )
                    s["final"] = assistant["content"]
                    if s.get("agent_mode") == "plan_execute" and any(
                        x["status"] != "done"
                        for x in s.get("plan", {}).get("steps", [])
                    ):
                        if s.get("plan_finish_retries", 0) < 2:
                            s["plan_finish_retries"] = (
                                s.get("plan_finish_retries", 0) + 1
                            )
                            self.store.event(
                                sid,
                                "plan_incomplete",
                                {"retry": s["plan_finish_retries"]},
                            )
                            s["status"] = "running"
                            self.store.save(s)
                            continue
                        s["status"] = "completed_unverified"
                        s["final"] += (
                            "\n[Plan incomplete: inspect unfinished steps before accepting.]"
                        )
                    break
        except StoreConflict:
            # Never overwrite the winner using the stale in-memory session.
            raise
        except ProviderError as exc:
            s["status"] = "provider_error"
            s["final"] = str(exc)
        except Exception as exc:
            s["status"] = "failed"
            s["final"] = "Runtime failure: " + type(exc).__name__
        if (
            self.deliberator
            and s.get("learning_enabled")
            and s["status"] in {"completed", "completed_unverified", "stopped"}
        ):
            if (
                s["usage_tokens"] < self.max_tokens
                and time.monotonic() - started < self.deadline
            ):
                try:
                    self.deliberator.reflect(s, self.store)
                except Exception as exc:
                    s["reflection_error"] = type(exc).__name__
            else:
                s["reflection_error"] = "budget_exhausted"
        self.store.save(s)
        self.store.event(
            sid, "stopped", {"status": s["status"], "verification": s["verification"]}
        )
        return s
