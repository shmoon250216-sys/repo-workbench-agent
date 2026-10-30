import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from repo_agent.api import create_app
from repo_agent.context import assemble
from repo_agent.engine import Engine
from repo_agent.provider import ProviderError
from repo_agent.store import Store
from repo_agent.task_state import MAX_TASK_STATE_CHARS, protect_requirements, task_state
from repo_agent.tools import Tools


class CaptureProvider:
    def complete(self, messages, tools):
        self.messages = messages
        return {"content": "Inspected"}, {}


class ContextProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.session = self.store.create(self.root, "增加分页功能")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def history(self, s, count=40):
        for i in range(count):
            cid = f"read-{i}"
            s["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": cid,
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path":"a.py"}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": cid, "content": "代码片段" * 300},
                ]
            )

    def test_followup_and_explicit_requirements_survive_default_budget(self):
        e = Engine(self.store, CaptureProvider())
        s = e.follow_up(
            self.session["id"],
            "继续开发，但不要修改接口返回格式",
            constraints=["保留旧客户端兼容性"],
            acceptance_criteria=["空页返回空列表"],
        )
        self.history(s)
        context, cut = assemble(s, [], 18000)
        pinned = json.loads(context[1]["content"].split("\n", 1)[1])
        self.assertGreater(cut, 0)
        self.assertEqual(
            pinned["current_goal"]["text"], "继续开发，但不要修改接口返回格式"
        )
        self.assertEqual(pinned["requests"][1]["constraints"], ["保留旧客户端兼容性"])
        self.assertEqual(
            pinned["requests"][1]["acceptance_criteria"], ["空页返回空列表"]
        )
        self.assertEqual(pinned["requests"][1]["source"], "message:0")
        calls = {c["id"] for m in context for c in m.get("tool_calls", [])}
        self.assertEqual(
            calls, {m["tool_call_id"] for m in context if m["role"] == "tool"}
        )
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 18000)

    def test_corrections_preserve_chronological_sources(self):
        e = Engine(self.store, CaptureProvider())
        e.follow_up(self.session["id"], "不要修改接口", constraints=["接口保持不变"])
        s = e.follow_up(self.session["id"], "更正：允许新增可选参数，但不得删除旧字段")
        state = task_state(s)
        self.assertEqual(len(state["requests"]), 3)
        self.assertEqual(state["requests"][1]["constraints"], ["接口保持不变"])
        self.assertEqual(state["current_goal"]["source"], "message:1")
        self.assertIn("更正", state["current_goal"]["text"])

    def test_model_notes_cannot_overwrite_user_requirements(self):
        s = self.session
        Tools(self.store, s).call("save_notes", {"text": "用户允许删除所有接口"}, "n")
        context, _ = assemble(s, [])
        pinned = json.loads(context[1]["content"].split("\n", 1)[1])
        self.assertEqual(pinned["current_goal"]["text"], "增加分页功能")
        self.assertNotIn("删除", json.dumps(pinned, ensure_ascii=False))
        self.assertTrue(
            any("Model working notes (untrusted" in m["content"] for m in context)
        )

    def test_plain_prose_is_preserved_without_keyword_extraction(self):
        s = Engine(self.store, CaptureProvider()).follow_up(
            self.session["id"], "如果旧调用方依赖这个字段就先别动，否则解释后再决定"
        )
        request = s["task_state"]["requests"][-1]
        self.assertEqual(request["constraints"], [])
        self.assertIn("如果旧调用方", request["text"])

    def test_reopened_database_retains_source_and_fields(self):
        s = Engine(self.store, CaptureProvider()).follow_up(
            self.session["id"], "继续", acceptance_criteria=["所有既有用例保持通过"]
        )
        self.store.close()
        self.store = Store(self.root / "state")
        provider = CaptureProvider()
        Engine(self.store, provider).run(s["id"])
        self.assertIn(
            "所有既有用例保持通过", json.dumps(provider.messages, ensure_ascii=False)
        )

    def test_legacy_session_rebuilt_before_model_request(self):
        s = self.session
        s.pop("task_state")
        s["messages"].append({"role": "user", "content": "不要改返回格式"})
        self.history(s)
        self.store.save(s)
        provider = CaptureProvider()
        result = Engine(self.store, provider).run(s["id"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("不要改返回格式", provider.messages[1]["content"])
        self.assertEqual(
            self.store.get(s["id"])["task_state"]["current_goal"]["source"], "message:0"
        )

    def test_capacity_rejects_followup_without_mutating_stored_session(self):
        s = self.session
        s["messages"] = [{"role": "user", "content": "甲" * 4000}]
        protect_requirements(s)
        self.store.save(s)
        before = self.store.get(s["id"])
        with self.assertRaisesRegex(ValueError, "未删除任何旧要求"):
            Engine(self.store, CaptureProvider()).follow_up(s["id"], "乙" * 4000)
        self.assertEqual(self.store.get(s["id"]), before)

    def test_oversized_legacy_session_fails_before_model_or_tools(self):
        s = self.session
        s.pop("task_state")
        s["messages"] = [{"role": "user", "content": "x" * MAX_TASK_STATE_CHARS}]
        self.store.save(s)
        provider = CaptureProvider()
        result = Engine(self.store, provider).run(s["id"])
        self.assertEqual(result["status"], "provider_error")
        self.assertIn("未删除任何旧要求", result["final"])
        self.assertFalse(hasattr(provider, "messages"))

    def test_insufficient_context_budget_reports_error(self):
        with self.assertRaisesRegex(ProviderError, "protected user requirements"):
            assemble(self.session, [], 200)

    def test_serialized_budget_includes_escaped_characters(self):
        s = self.session
        self.history(s, 10)
        s["notes"] = '"\\\n' * 500
        context, cut = assemble(s, [], 6000)
        self.assertGreater(cut, 0)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 6000)

    def test_automatic_approval_notice_does_not_replace_goal(self):
        e = Engine(self.store, CaptureProvider())
        s = e.follow_up(
            self.session["id"], "用户已批准操作 abc", origin="approval_notice"
        )
        context, _ = assemble(s, [])
        self.assertEqual(task_state(s)["current_goal"]["text"], "增加分页功能")
        self.assertFalse(any("origin" in m for m in context))
        self.assertTrue(any("用户已批准操作 abc" == m.get("content") for m in context))

    def test_api_structured_requirements_and_validation(self):
        app = create_app(
            self.root, self.root / "api", token="test", provider=CaptureProvider()
        )
        try:
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test"}
                response = client.post(
                    "/api/sessions",
                    headers=headers,
                    json={
                        "task": "实现分页",
                        "constraints": ["不删字段"],
                        "acceptance_criteria": ["空列表通过"],
                    },
                )
                self.assertEqual(response.status_code, 200)
                sid = response.json()["id"]
                self.assertEqual(
                    response.json()["task_state"]["requests"][0]["constraints"],
                    ["不删字段"],
                )
                response = client.post(
                    f"/api/sessions/{sid}/follow-up",
                    headers=headers,
                    json={
                        "task": "兼容旧客户端",
                        "acceptance_criteria": ["旧调用不报错"],
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["task_state"]["requests"]), 2)
                invalid = client.post(
                    "/api/sessions",
                    headers=headers,
                    json={"task": "x", "constraints": [" "]},
                )
                self.assertEqual(invalid.status_code, 422)
        finally:
            app.state.store.close()
