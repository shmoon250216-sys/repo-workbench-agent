import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from repo_agent.api import create_app
from repo_agent.context import assemble
from repo_agent.demo import ScriptedDemo, create_fixture
from repo_agent.engine import Engine
from repo_agent.policy import Denied
from repo_agent.provider import ChatProvider, ProviderError
from repo_agent.store import Store
from repo_agent.tools import ApprovalNeeded, Tools


def call(name, args, cid="one"):
    return {
        "content": "",
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


class Sequence:
    def __init__(self, *messages):
        self.messages = iter(messages)

    def complete(self, *_):
        return next(self.messages), {}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.s = self.store.create(self.root, "Explain or improve this repository")
        self.tools = Tools(self.store, self.s)
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def tool(self, name, args, cid="one"):
        return self.tools.call(name, args, cid)

    def proposal(self):
        return self.tool(
            "propose_edit",
            {"path": "a.py", "old_text": "value = 1", "new_text": "value = 2"},
        )["proposal_id"]

    def test_paths_reject_traversal_absolute_and_credentials(self):
        for path in [
            "../secret",
            "/etc/passwd",
            "C:/Windows/test",
            ".git/config",
            ".env",
            ".env.local",
            "keys.pem",
            ".ssh/id_rsa",
            "a.py:stream",
        ]:
            with self.subTest(path=path), self.assertRaises(Denied):
                self.tools.policy.path(path)

    def test_hardlink_blocked(self):
        os.link(self.root / "a.py", self.root / "alias.py")
        with self.assertRaises(Denied):
            self.tools.policy.path("alias.py")

    def test_symlink_blocked(self):
        try:
            os.symlink(self.root / "a.py", self.root / "alias.py")
        except OSError:
            self.skipTest("Host cannot create symlinks")
        with self.assertRaises(Denied):
            self.tools.policy.path("alias.py")

    def test_listing_hides_protected_files(self):
        (self.root / ".env").write_text("secret")
        (self.root / ".git").mkdir()
        (self.root / ".git/config").write_text("secret")
        self.assertEqual(["a.py"], self.tool("list_files", {})["files"])

    def test_read_line_range(self):
        (self.root / "a.py").write_text("a\nb\nc\n")
        self.assertEqual(
            "2: b",
            self.tool("read_file", {"path": "a.py", "start": 2, "lines": 1})["text"],
        )

    def test_search_is_literal_not_executable(self):
        self.assertEqual([], self.tool("search_code", {"query": ".*"})["matches"])
        self.assertEqual(
            1, len(self.tool("search_code", {"query": "value"})["matches"])
        )

    def test_strict_tool_schema(self):
        for args in [{"path": "a.py", "lines": "10"}, {"path": "a.py", "extra": True}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool("read_file", args)

    def test_unknown_tool_blocked(self):
        with self.assertRaises(ValueError):
            self.tool("shell", {"command": "whoami"})

    def test_skills_discovery_is_metadata_only(self):
        skills = self.tools.skills()
        self.assertEqual(4, len(skills))
        self.assertEqual({"name", "description"}, set(skills[0]))
        self.assertIn(
            "instructions", self.tool("load_skill", {"name": "bug-diagnosis"})
        )
        with self.assertRaises(ValueError):
            self.tool("load_skill", {"name": "../private"})

    def test_proposal_does_not_modify_file(self):
        self.proposal()
        self.assertEqual("value = 1\n", (self.root / "a.py").read_text())

    def test_edit_requires_human_approval(self):
        aid = self.proposal()
        with self.assertRaises(ApprovalNeeded):
            self.tool("apply_edit", {"proposal_id": aid})
        self.store.resolve(self.s["id"], aid, True)
        self.tool("apply_edit", {"proposal_id": aid})
        self.assertEqual("value = 2\n", (self.root / "a.py").read_text())

    def test_rejected_edit_preserves_file(self):
        aid = self.proposal()
        self.store.resolve(self.s["id"], aid, False)
        with self.assertRaises(Denied):
            self.tool("apply_edit", {"proposal_id": aid})
        self.assertEqual("value = 1\n", (self.root / "a.py").read_text())

    def test_stale_approval_rejected(self):
        aid = self.proposal()
        self.store.resolve(self.s["id"], aid, True)
        (self.root / "a.py").write_text("value = 3\n")
        with self.assertRaises(ValueError):
            self.tool("apply_edit", {"proposal_id": aid})

    def test_approval_cannot_cross_sessions(self):
        aid = self.proposal()
        other = self.store.create(self.root, "other")
        with self.assertRaises(Denied):
            Tools(self.store, other).call("apply_edit", {"proposal_id": aid}, "other")

    def test_edit_replay_is_idempotent(self):
        aid = self.proposal()
        self.store.resolve(self.s["id"], aid, True)
        self.tool("apply_edit", {"proposal_id": aid})
        self.assertTrue(self.tool("apply_edit", {"proposal_id": aid})["replayed"])

    def test_ambiguous_replacement_rejected(self):
        (self.root / "a.py").write_text("x x")
        with self.assertRaises(ValueError):
            self.tool(
                "propose_edit", {"path": "a.py", "old_text": "x", "new_text": "y"}
            )

    def test_new_file_requires_missing_path(self):
        with self.assertRaises(ValueError):
            self.tool(
                "propose_edit", {"path": "a.py", "old_text": "", "new_text": "new"}
            )

    def test_large_result_externalized(self):
        (self.root / "a.py").write_text("a" * 8000)
        result = self.tool("read_file", {"path": "a.py"})
        self.assertTrue(result["truncated"])
        self.assertIn("artifact_id", result)
        self.s["messages"] = [{"role": "tool", "content": json.dumps(result)}]
        self.assertEqual(
            4000,
            len(
                self.tool("read_artifact", {"artifact_id": result["artifact_id"]})[
                    "text"
                ]
            ),
        )

    def test_artifact_isolation(self):
        aid = self.store.artifact("secret", "other-session")
        self.s["messages"] = [{"role": "user", "content": "read " + aid}]
        with self.assertRaises(Denied):
            self.tool("read_artifact", {"artifact_id": aid})

    def test_disabled_execution_fails_closed(self):
        with self.assertRaises(Denied):
            self.tool("run_tests", {})

    def test_local_tests_require_separate_approval(self):
        self.tools.backend = "trusted-local"
        with self.assertRaises(ApprovalNeeded):
            self.tool("run_tests", {})

    def test_zero_tests_not_reported_passed(self):
        (self.root / "tests").mkdir()
        self.tools.backend = "trusted-local"
        with self.assertRaises(ApprovalNeeded) as cm:
            self.tool("run_tests", {})
        self.store.resolve(self.s["id"], cm.exception.aid, True)
        self.assertEqual("not_run", self.tool("run_tests", {})["status"])

    def test_docker_missing_does_not_fallback(self):
        self.tools.backend = "docker"
        with self.assertRaises(ApprovalNeeded) as cm:
            self.tool("run_tests", {})
        self.store.resolve(self.s["id"], cm.exception.aid, True)
        with (
            patch("repo_agent.tools.shutil.which", return_value=None),
            self.assertRaises(Denied),
        ):
            self.tool("run_tests", {})

    def test_dynamic_tool_result_then_final(self):
        engine = Engine(
            self.store,
            Sequence(call("read_file", {"path": "a.py"}), {"content": "value is 1"}),
        )
        s = engine.run(self.s["id"])
        self.assertEqual("completed", s["status"])
        self.assertEqual(2, s["steps"])
        self.assertIn("value = 1", s["messages"][1]["content"])

    def test_invalid_tool_is_feedback_not_runtime_crash(self):
        s = Engine(
            self.store,
            Sequence(call("unknown", {}), {"content": "Tool was unavailable"}),
        ).run(self.s["id"])
        self.assertEqual("completed", s["status"])
        self.assertIn("Unknown tool", s["messages"][1]["content"])

    def test_duplicate_call_id_stops(self):
        s = Engine(
            self.store, Sequence(call("list_files", {}), call("list_files", {}))
        ).run(self.s["id"])
        self.assertEqual("provider_error", s["status"])

    def test_step_budget_stops_loop(self):
        self.s["max_steps"] = 1
        self.store.save(self.s)
        s = Engine(self.store, Sequence(call("list_files", {}))).run(self.s["id"])
        self.assertEqual("budget_exhausted", s["status"])

    def test_approval_resume_survives_new_engine(self):
        aid = self.proposal()
        s = Engine(self.store, Sequence(call("apply_edit", {"proposal_id": aid}))).run(
            self.s["id"]
        )
        self.assertEqual("waiting_approval", s["status"])
        self.store.resolve(self.s["id"], aid, True)
        s = Engine(self.store, Sequence({"content": "Modified; tests not run"})).run(
            self.s["id"]
        )
        self.assertEqual("completed_unverified", s["status"])
        self.assertEqual(1, s["revision"])
        self.assertEqual("not_run", s["verification"])

    def test_interrupted_test_is_not_blindly_replayed(self):
        self.s["messages"] = [dict(role="assistant", **call("run_tests", {}))]
        self.store.save(self.s)
        self.store.put_call(self.s["id"], "one", "running")
        s = Engine(
            self.store,
            Sequence({"content": "Test outcome unknown"}),
            backend="trusted-local",
        ).run(self.s["id"])
        self.assertIn("interrupted", s["messages"][1]["content"])
        self.assertEqual("error", s["verification"])

    def test_cached_result_restores_missing_message(self):
        self.s["messages"] = [dict(role="assistant", **call("list_files", {}))]
        self.store.save(self.s)
        self.store.put_call(self.s["id"], "one", "done", {"files": ["cached.py"]})
        s = Engine(self.store, Sequence({"content": "done"})).run(self.s["id"])
        self.assertIn("cached.py", s["messages"][1]["content"])

    def test_context_keeps_tool_pairs_and_task(self):
        for i in range(20):
            self.s["messages"] += [
                dict(role="assistant", **call("list_files", {}, str(i))),
                {"role": "tool", "tool_call_id": str(i), "content": "x" * 600},
            ]
        context, cut = assemble(self.s, self.tools.skills(), 6000)
        self.assertGreater(cut, 0)
        self.assertIn(self.s["task"], context[0]["content"])
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 6000)
        calls = {c["id"] for m in context for c in m.get("tool_calls", [])}
        self.assertEqual(
            calls, {m["tool_call_id"] for m in context if m["role"] == "tool"}
        )

    def test_real_subprocess_demo(self):
        root = self.base / "fixture"
        create_fixture(root)
        s = self.store.create(root, "Fix addition")
        engine = Engine(self.store, ScriptedDemo(), backend="trusted-local")
        for _ in range(4):
            s = engine.run(s["id"])
            if s["status"] != "waiting_approval":
                break
            self.store.resolve(s["id"], s["pending_approval"], True)
        self.assertEqual("completed", s["status"])
        self.assertEqual("passed", s["verification"])
        self.assertEqual(1, s["revision"])

    def test_api_auth_and_review(self):
        app = create_app(
            self.root,
            self.base / "api",
            token="test-token",
            provider=Sequence({"content": "done"}),
        )
        with TestClient(app) as client:
            self.assertEqual(401, client.get("/api/sessions").status_code)
            headers = {"Authorization": "Bearer test-token"}
            s = client.post(
                "/api/sessions", headers=headers, json={"task": "Explain"}
            ).json()
            self.assertEqual(
                200,
                client.post(
                    "/api/sessions/" + s["id"] + "/run", headers=headers, json={}
                ).status_code,
            )
            self.assertEqual(
                "completed",
                client.get("/api/sessions/" + s["id"], headers=headers).json()[
                    "status"
                ],
            )
            self.assertEqual(
                422,
                client.post(
                    "/api/sessions", headers=headers, json={"task": "x", "root": "/etc"}
                ).status_code,
            )
        app.state.store.db.close()

    def test_followup_preserves_history(self):
        e = Engine(
            self.store,
            Sequence({"content": "Initial answer"}, {"content": "Follow-up answer"}),
        )
        e.run(self.s["id"])
        e.follow_up(self.s["id"], "Explain the boundary case")
        result = e.run(self.s["id"])
        self.assertEqual("completed", result["status"])
        self.assertEqual("user", result["messages"][-2]["role"])
        self.assertEqual(2, result["steps"])

    def test_followup_rejected_during_approval(self):
        aid = self.proposal()
        e = Engine(self.store, Sequence(call("apply_edit", {"proposal_id": aid})))
        e.run(self.s["id"])
        with self.assertRaises(ValueError):
            e.follow_up(self.s["id"], "Ignore approval")

    def test_fake_success_is_not_verified(self):
        self.s["revision"] = 1
        self.store.save(self.s)
        s = Engine(self.store, Sequence({"content": "All tests passed!"})).run(
            self.s["id"]
        )
        self.assertEqual("completed_unverified", s["status"])
        self.assertEqual("not_run", s["verification"])

    def test_repeated_identical_tool_failure_stops(self):
        e = Engine(
            self.store, Sequence(*[call("missing", {}, str(i)) for i in range(3)])
        )
        s = e.run(self.s["id"])
        self.assertEqual("stopped", s["status"])
        self.assertIn("Repeated", s["final"])

    def test_context_oversized_exchange_is_explicit(self):
        self.s["messages"] = [
            dict(role="assistant", **call("propose_edit", {"new_text": "x" * 25000}))
        ]
        with self.assertRaises(ProviderError):
            assemble(self.s, self.tools.skills(), 6000)

    def test_cooperative_stop_before_tools(self):
        engine = None

        class StopProvider:
            def complete(inner, *_):
                engine.cancel(self.s["id"])
                return call("list_files", {}), {}

        engine = Engine(self.store, StopProvider())
        s = engine.run(self.s["id"])
        self.assertEqual("stopped", s["status"])
        self.assertFalse(
            any(e["kind"] == "tool_start" for e in self.store.events(s["id"]))
        )


class ProviderTests(unittest.TestCase):
    def provider(self):
        return ChatProvider(
            "https://example.invalid/v1", "fixture", "secret", attempts=2
        )

    def test_credential_missing(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ProviderError):
            ChatProvider().complete([], [])

    def test_auth_error_not_retried_or_logged(self):
        error = urllib.error.HTTPError("url", 401, "secret", {}, io.BytesIO(b"secret"))
        with (
            patch("urllib.request.urlopen", side_effect=error) as mock,
            self.assertRaises(ProviderError) as cm,
        ):
            self.provider().complete([], [])
        self.assertEqual(1, mock.call_count)
        self.assertNotIn("secret", str(cm.exception))

    def test_transient_error_has_bounded_retry(self):
        error = urllib.error.HTTPError("url", 503, "unavailable", {}, None)
        with (
            patch("urllib.request.urlopen", side_effect=error) as mock,
            patch("time.sleep"),
            self.assertRaises(ProviderError),
        ):
            self.provider().complete([], [])
        self.assertEqual(2, mock.call_count)

    def test_malformed_response(self):
        response = io.BytesIO(b"{}")
        with (
            patch("urllib.request.urlopen", return_value=response),
            self.assertRaises(ProviderError),
        ):
            self.provider().complete([], [])


if __name__ == "__main__":
    unittest.main()
