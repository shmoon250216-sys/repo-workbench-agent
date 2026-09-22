import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from repo_agent.api import create_app
from repo_agent.demo import ScriptedDemo, create_fixture
from repo_agent.provider import ChatProvider, ProviderError


def wire(*objects, done=True):
    text = "".join("data: " + json.dumps(x) + "\n\n" for x in objects)
    if done:
        text += "data: [DONE]\n\n"
    return io.BytesIO(text.encode())


def delta(value):
    return {"choices": [{"index": 0, "delta": value}]}


class StreamingTests(unittest.TestCase):
    def test_text_and_fragmented_tools(self):
        response = wire(
            delta({"content": "正在"}),
            delta({"content": "读取"}),
            delta(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "read_file", "arguments": '{"pa'},
                        }
                    ]
                }
            ),
            delta(
                {"tool_calls": [{"index": 0, "function": {"arguments": 'th":"a.py"}'}}]}
            ),
            {"choices": [], "usage": {"total_tokens": 14}},
        )
        parts = []
        with patch("urllib.request.urlopen", return_value=response):
            msg, usage = ChatProvider(
                "https://example.invalid", "test", "secret"
            ).complete_stream([], [], parts.append)
        self.assertEqual(["正在", "读取"], parts)
        self.assertEqual(
            {"path": "a.py"}, json.loads(msg["tool_calls"][0]["function"]["arguments"])
        )
        self.assertEqual(14, usage["total_tokens"])

    def test_incomplete_stream_not_retried(self):
        with patch(
            "urllib.request.urlopen",
            return_value=wire(delta({"content": "partial"}), done=False),
        ) as request:
            with self.assertRaises(ProviderError):
                ChatProvider(
                    "https://example.invalid", "test", "secret"
                ).complete_stream([], [], lambda _: None)
        self.assertEqual(1, request.call_count)

    def test_partial_tool_does_not_return(self):
        response = wire(
            delta(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "x",
                            "function": {"name": "apply_edit", "arguments": "{"},
                        }
                    ]
                }
            ),
            done=False,
        )
        with (
            patch("urllib.request.urlopen", return_value=response),
            self.assertRaises(ProviderError),
        ):
            ChatProvider("https://example.invalid", "test", "secret").complete_stream(
                [], [], lambda _: None
            )


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        create_fixture(self.root / "workspace")
        self.app = create_app(
            self.root / "workspace",
            self.root / "state",
            token="local-password",
            provider=ScriptedDemo(),
            backend="trusted-local",
        )
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.app.state.store.db.close()
        self.tmp.cleanup()

    def login(self):
        return self.client.post("/api/login", json={"password": "local-password"})

    def test_cookie_login_and_logout(self):
        self.assertEqual(401, self.client.get("/api/config").status_code)
        r = self.login()
        self.assertIn("HttpOnly", r.headers["set-cookie"])
        self.assertIn("SameSite=strict", r.headers["set-cookie"])
        self.assertEqual(200, self.client.get("/api/config").status_code)
        self.client.post("/api/logout", json={})
        self.assertEqual(401, self.client.get("/api/config").status_code)

    def test_bad_login_and_cross_origin(self):
        self.assertEqual(
            401,
            self.client.post("/api/login", json={"password": "错误密码"}).status_code,
        )
        self.assertEqual(
            403,
            self.client.post(
                "/api/login",
                json={"password": "local-password"},
                headers={"Origin": "https://evil.invalid"},
            ).status_code,
        )
        self.login()
        self.assertEqual(
            403,
            self.client.post(
                "/api/sessions",
                json={"task": "do something"},
                headers={"Origin": "https://evil.invalid"},
            ).status_code,
        )

    def test_approval_auto_continues_and_duplicate_rejected(self):
        self.login()
        s = self.client.post("/api/sessions", json={"task": "Fix addition"}).json()
        url = "/api/sessions/" + s["id"]
        self.client.post(url + "/run", json={})
        s = self.client.get(url).json()
        edit = s["pending_approval"]
        r = self.client.post(url + "/approvals/" + edit, json={"allow": True})
        self.assertTrue(r.json()["auto_resumed"])
        s = self.client.get(url).json()
        self.assertEqual("waiting_approval", s["status"])
        self.assertNotEqual(edit, s["pending_approval"])
        self.client.post(
            url + "/approvals/" + s["pending_approval"], json={"allow": True}
        )
        s = self.client.get(url).json()
        self.assertEqual("completed", s["status"])
        self.assertEqual("passed", s["verification"])
        self.assertEqual(
            409,
            self.client.post(
                url + "/approvals/" + edit, json={"allow": True}
            ).status_code,
        )
        self.assertEqual(1, s["revision"])

    def test_approval_resumes_model_that_stopped_after_proposal(self):
        class ProposalThenStop:
            def complete(inner, messages, tools):
                results = [
                    json.loads(m["content"]) for m in messages if m["role"] == "tool"
                ]
                follow = any(
                    m["role"] == "user" and "操作" in m["content"] for m in messages
                )
                if not results:
                    name, args, cid = (
                        "propose_edit",
                        {
                            "path": "calculator.py",
                            "old_text": "return a - b",
                            "new_text": "return a + b",
                        },
                        "p",
                    )
                elif follow and len(results) == 1:
                    name, args, cid = (
                        "apply_edit",
                        {"proposal_id": results[0]["proposal_id"]},
                        "a",
                    )
                else:
                    return {
                        "content": "Please approve"
                        if not follow
                        else "Applied; tests not run"
                    }, {}
                return {
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ]
                }, {}

        self.app.state.engine.provider = ProposalThenStop()
        self.login()
        s = self.client.post("/api/sessions", json={"task": "Fix addition"}).json()
        url = "/api/sessions/" + s["id"]
        self.client.post(url + "/run", json={})
        s = self.client.get(url).json()
        self.assertEqual("completed", s["status"])
        aid = s["approvals"][0]["id"]
        self.client.post(url + "/approvals/" + aid, json={"allow": True})
        s = self.client.get(url).json()
        self.assertEqual("completed_unverified", s["status"])
        self.assertEqual(1, s["revision"])

    def test_events_auth_and_cursor_replay(self):
        self.login()
        s = self.client.post("/api/sessions", json={"task": "Fix addition"}).json()
        url = "/api/sessions/" + s["id"]
        self.client.post(url + "/run", json={})
        s = self.client.get(url).json()
        cursor = s["events"][-2]["id"]
        result = self.client.get(
            url + "/events", headers={"Last-Event-ID": str(cursor)}
        )
        self.assertIn("text/event-stream", result.headers["content-type"])
        self.assertIn("event: idle", result.text)
        event_ids = [
            int(line[4:])
            for line in result.text.splitlines()
            if line.startswith("id: ")
        ]
        self.assertTrue(event_ids)
        self.assertTrue(all(n > cursor for n in event_ids))
        self.client.post("/api/logout", json={})
        self.assertEqual(401, self.client.get(url + "/events").status_code)


if __name__ == "__main__":
    unittest.main()
