import concurrent.futures
import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_agent.engine import Engine
from repo_agent.execution import build_plan, digest, workspace_snapshot
from repo_agent.policy import Denied
from repo_agent.store import Store
from repo_agent.tools import ApprovalNeeded, Tools


class FakeProcess:
    def __init__(self, *args, **kwargs):
        pass

    def wait(self, **kwargs):
        return 0


class ExecutionApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        (self.root / "code.py").write_text("value = 1\n")
        self.store = Store(self.base / "state")
        self.session = self.store.create(self.root, "Run tests")
        self.tools = Tools(self.store, self.session, backend="trusted-local")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def request(self, runner="unittest"):
        with self.assertRaises(ApprovalNeeded) as exc:
            self.tools.call("run_tests", {"runner": runner}, "call1")
        return exc.exception.aid

    def approve(self):
        aid = self.request()
        self.store.resolve(self.session["id"], aid, True)
        return aid

    def test_unchanged_pending_spec_reuses_approval(self):
        aid = self.request()
        self.assertEqual(aid, self.request())
        self.assertEqual(len(self.store.approvals(self.session["id"])), 1)

    def test_exact_command_executes_once_and_consumes_approval(self):
        aid = self.approve()
        approved = self.store.approvals(self.session["id"])[0]["data"]
        with patch(
            "repo_agent.tools.subprocess.Popen", side_effect=FakeProcess
        ) as spawn:
            self.tools.call("run_tests", {}, "call1")
            self.assertEqual(spawn.call_args.args[0], approved["spec"]["command"])
            self.assertEqual(
                digest(spawn.call_args.kwargs["env"]),
                approved["spec"]["environment_sha256"],
            )
            with self.assertRaises(Denied):
                self.tools.call("run_tests", {}, "call1")
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(
            self.store.approvals(self.session["id"])[0]["decision"], "consumed"
        )
        with self.assertRaises(ValueError):
            self.store.resolve(self.session["id"], aid, True)

    def test_code_changes_require_new_approval_and_do_not_spawn(self):
        old = self.approve()
        (self.root / "code.py").write_text("value = 2\n")
        with patch("repo_agent.tools.subprocess.Popen") as spawn:
            new = self.request()
            spawn.assert_not_called()
        self.assertNotEqual(old, new)
        rows = self.store.approvals(self.session["id"])
        self.assertEqual([r["decision"] for r in rows], ["stale", "pending"])
        self.assertEqual(rows[1]["data"]["supersedes"], old)

    def test_changed_runner_invalidates_previous_command(self):
        old = self.approve()
        self.assertNotEqual(old, self.request("pytest"))

    def test_changed_process_environment_requires_approval(self):
        old = self.approve()
        with patch.dict(os.environ, {"LANG": "changed-for-approval-test"}):
            self.assertNotEqual(old, self.request())

    def test_interpreter_identity_change_requires_approval(self):
        old = self.approve()
        from repo_agent.execution import executable_identity

        def changed(value, env):
            identity = executable_identity(value, env)
            identity["sha256"] = "different"
            return identity

        with patch("repo_agent.execution.executable_identity", side_effect=changed):
            self.assertNotEqual(old, self.request())

    def test_backend_change_cannot_reuse_docker_approval(self):
        self.tools.backend = "docker"
        with (
            patch(
                "repo_agent.execution.executable_identity",
                return_value={"path": "docker", "sha256": "mock"},
            ),
            patch(
                "repo_agent.execution.docker_metadata",
                return_value={
                    "image_id": "sha256:" + "a" * 64,
                    "daemon_id": "test-daemon",
                },
            ),
        ):
            old = self.approve()
            spec = self.store.approvals(self.session["id"])[0]["data"]["spec"]
            self.assertIn("sha256:" + "a" * 64, spec["command"])
            self.assertIn("--pull=never", spec["command"])
        self.tools.backend = "trusted-local"
        with patch("repo_agent.tools.subprocess.Popen") as spawn:
            self.assertNotEqual(old, self.request())
            spawn.assert_not_called()

    def test_docker_image_change_requires_new_approval(self):
        self.tools.backend = "docker"
        with (
            patch(
                "repo_agent.execution.executable_identity",
                return_value={"path": "docker", "sha256": "mock"},
            ),
            patch("repo_agent.execution.docker_metadata") as metadata,
        ):
            metadata.return_value = {
                "image_id": "sha256:" + "a" * 64,
                "daemon_id": "same",
            }
            old = self.approve()
            metadata.return_value = {
                "image_id": "sha256:" + "b" * 64,
                "daemon_id": "same",
            }
            self.assertNotEqual(old, self.request())

    def test_reverting_source_does_not_revive_stale_allow(self):
        old = self.approve()
        (self.root / "code.py").write_text("value = 2\n")
        intermediate = self.request()
        (self.root / "code.py").write_text("value = 1\n")
        self.assertNotIn(self.request(), {old, intermediate})

    def test_legacy_approval_requires_new_review(self):
        legacy = hashlib.md5(
            (self.session["id"] + "call1").encode(), usedforsecurity=False
        ).hexdigest()
        self.store.approval(
            self.session["id"],
            "tests",
            {"backend": "docker", "runner": "unittest"},
            legacy,
        )
        self.store.resolve(self.session["id"], legacy, True)
        self.assertNotEqual(legacy, self.request())
        self.assertEqual(
            self.store.approvals(self.session["id"])[0]["decision"], "stale"
        )

    def test_second_snapshot_catches_change_before_spawn(self):
        self.approve()
        count = 0

        def changed(*args):
            nonlocal count
            count += 1
            if count == 2:
                (self.root / "code.py").write_text("changed during approval check\n")
            return build_plan(*args)

        with (
            patch("repo_agent.tools.build_plan", side_effect=changed),
            patch("repo_agent.tools.subprocess.Popen") as spawn,
        ):
            self.request()
            spawn.assert_not_called()

    def test_new_and_deleted_files_change_snapshot_but_cache_does_not(self):
        initial = workspace_snapshot(self.root, self.store.directory)["sha256"]
        (self.root / "new.py").write_text("new")
        added = workspace_snapshot(self.root, self.store.directory)["sha256"]
        self.assertNotEqual(initial, added)
        (self.root / "new.py").unlink()
        (self.root / "__pycache__").mkdir()
        (self.root / "__pycache__/ignored.pyc").write_bytes(b"cache")
        self.assertEqual(
            initial, workspace_snapshot(self.root, self.store.directory)["sha256"]
        )

    def test_oversize_snapshot_fails_instead_of_truncating(self):
        with (self.root / "large.bin").open("wb") as stream:
            stream.truncate(8_000_001)
        with self.assertRaises(Denied):
            self.request()

    def test_claim_is_bound_to_session(self):
        aid = self.approve()
        spec = self.store.approvals(self.session["id"])[0]["data"]["spec"]
        with self.assertRaises(ValueError):
            self.store.consume_test_approval("different-session", aid, spec)

    def test_atomic_claim_across_independent_store_connections(self):
        aid = self.approve()
        spec = self.store.approvals(self.session["id"])[0]["data"]["spec"]
        other = Store(self.base / "state")
        barrier = threading.Barrier(2)

        def claim(store):
            barrier.wait(timeout=5)
            try:
                store.consume_test_approval(self.session["id"], aid, spec)
                return True
            except ValueError:
                return False

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(claim, store) for store in (self.store, other)]
                self.assertEqual(sum(f.result(timeout=10) for f in futures), 1)
        finally:
            other.close()

    def test_approved_spec_survives_database_reopen(self):
        self.approve()
        self.store.close()
        self.store = Store(self.base / "state")
        self.tools = Tools(self.store, self.session, backend="trusted-local")
        with patch(
            "repo_agent.tools.subprocess.Popen", side_effect=FakeProcess
        ) as spawn:
            self.tools.call("run_tests", {}, "call1")
            self.assertEqual(spawn.call_count, 1)

    def test_engine_pauses_again_when_code_changed_then_recovers(self):
        class Model:
            count = 0

            def complete(inner, *args):
                inner.count += 1
                if inner.count == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call1",
                                "type": "function",
                                "function": {"name": "run_tests", "arguments": "{}"},
                            }
                        ],
                    }, {}
                return {"content": "Finished"}, {}

        engine = Engine(self.store, Model(), backend="trusted-local")
        s = engine.run(self.session["id"])
        old = s["pending_approval"]
        self.store.resolve(s["id"], old, True)
        (self.root / "code.py").write_text("new content")
        with patch(
            "repo_agent.tools.subprocess.Popen", side_effect=FakeProcess
        ) as spawn:
            s = engine.run(s["id"])
            self.assertEqual(s["status"], "waiting_approval")
            self.assertNotEqual(s["pending_approval"], old)
            spawn.assert_not_called()
            self.store.resolve(s["id"], s["pending_approval"], True)
            s = engine.run(s["id"])
            self.assertEqual(s["status"], "completed")
            self.assertEqual(spawn.call_count, 1)
