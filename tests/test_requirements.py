import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from repo_agent.api import create_app
from repo_agent.context import assemble
from repo_agent.engine import Engine
from repo_agent.requirements import (
    ExtractionError,
    RequirementExtractor,
    RequirementModel,
    configured_extractor,
    project,
)
from repo_agent.store import Store
from repo_agent.task_state import protect_requirements, task_state
from repo_agent.tools import Tools


def item(kind, text, source="initial", **kw):
    return {"kind": kind, "text": text, "source": source, "quote": text, **kw}


class FakeModel:
    def __init__(self, payload):
        self.payload, self.calls = payload, 0

    def extract(self, previous, requests):
        self.calls += 1
        self.previous, self.requests = previous, requests
        return copy.deepcopy(self.payload), {"total_tokens": 123}


class MainModel:
    def complete(self, messages, tools):
        self.messages = messages
        return {"content": "Done; no edits"}, {}


class RequirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.text = "增加分页。不得修改数据库。默认20条。" + "业务背景说明。" * 250
        self.s = self.store.create(self.root, self.text, semantic_requirements=True)
        self.payload = {
            "covered_sources": ["initial"],
            "items": [
                item("goal", "增加分页"),
                item("constraint", "不得修改数据库"),
                item("acceptance", "默认20条"),
            ],
        }
        self.model = FakeModel(self.payload)
        self.extractor = RequirementExtractor(self.model)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def prepare(self):
        return self.extractor.prepare(self.s, task_state(self.s))

    def test_valid_projection_shorter_and_no_duplicate_raw_history(self):
        result = self.prepare()
        self.assertEqual(result["status"], "extracted")
        self.assertEqual(result["usage_tokens"], 123)
        projection, covered = project(self.s, task_state(self.s))
        self.assertEqual(covered, {"initial"})
        self.assertLess(
            len(json.dumps(projection)), len(json.dumps(task_state(self.s)))
        )
        context, _ = assemble(self.s, [])
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("业务背景说明", serialized)
        self.assertIn("不得修改数据库", serialized)
        self.assertEqual(self.s["task"], self.text)

    def test_cached_view_survives_reopen_and_no_second_extraction(self):
        self.prepare()
        self.store.save(self.s)
        self.store.close()
        self.store = Store(self.root / "state")
        self.s = self.store.get(self.s["id"])
        self.assertEqual(self.prepare()["status"], "cached")
        self.assertEqual(self.model.calls, 1)

    def test_incremental_correction_preserves_other_conditions_and_audit(self):
        self.prepare()
        self.s["messages"].append(
            {"role": "user", "content": "更正：默认50条，其他不变。"}
        )
        protect_requirements(self.s)
        self.model.payload = {
            "covered_sources": ["message:0"],
            "items": [
                item(
                    "acceptance",
                    "默认50条",
                    "message:0",
                    replaces=["v1-2"],
                    correction_quote="更正：默认50条",
                )
            ],
        }
        self.assertEqual(self.prepare()["status"], "extracted")
        texts = [r["text"] for r in self.s["requirements_view"]["items"]]
        self.assertIn("不得修改数据库", texts)
        self.assertIn("默认50条", texts)
        self.assertNotIn("默认20条", texts)
        self.assertEqual(
            self.s["requirements_versions"][-1]["retired"][0]["text"], "默认20条"
        )
        context, _ = assemble(self.s, [])
        self.assertFalse(
            any(m.get("content") == "更正：默认50条，其他不变。" for m in context)
        )

    def test_invalid_payloads_retain_raw_state(self):
        changes = [
            {"covered_sources": ["fake"], "items": self.payload["items"]},
            {"covered_sources": ["initial"], "items": [item("goal", "虚构原文")]},
            {"covered_sources": ["initial"], "items": [item("shell", "增加分页")]},
            {"covered_sources": ["initial"], "items": []},
            {
                "covered_sources": ["initial"],
                "items": [
                    item(
                        "goal",
                        "增加分页",
                        replaces=["missing"],
                        correction_quote="增加分页",
                    )
                ],
            },
        ]
        for payload in changes:
            with self.subTest(payload=payload):
                self.model.payload = payload
                self.assertEqual(self.prepare()["status"], "fallback")
                self.assertNotIn("requirements_view", self.s)
                context, _ = assemble(self.s, [])
                self.assertIn(self.text, context[1]["content"])

    def test_failed_update_keeps_previous_view_and_new_user_raw(self):
        self.prepare()
        before = copy.deepcopy(self.s["requirements_view"])
        self.s["messages"].append({"role": "user", "content": "不要改接口字段"})
        protect_requirements(self.s)
        self.model.payload = {
            "covered_sources": ["message:0"],
            "items": [item("constraint", "虚构", "message:0")],
        }
        self.assertEqual(self.prepare()["status"], "fallback")
        self.assertEqual(before, self.s["requirements_view"])
        context, _ = assemble(self.s, [])
        self.assertIn("不要改接口字段", context[1]["content"])

    def test_changed_source_invalidates_cached_view(self):
        self.prepare()
        self.s["task"] = "不同任务"
        projection, covered = project(self.s, task_state(self.s))
        self.assertEqual(covered, set())
        self.assertEqual(projection["current_goal"]["text"], "不同任务")

    def test_short_request_uses_raw_without_model(self):
        s = self.store.create(self.root, "解释入口")
        result = self.extractor.prepare(s, task_state(s))
        self.assertEqual(result["status"], "raw_short_input")
        self.assertEqual(self.model.calls, 0)

    def test_disable_extraction_does_not_use_saved_projection(self):
        self.prepare()
        self.store.save(self.s)
        main = MainModel()
        Engine(self.store, main).run(self.s["id"])
        self.assertIn(self.text, main.messages[1]["content"])

    def test_network_failure_falls_back_without_erasing_sources(self):
        failing = RequirementExtractor(
            RequirementModel("https://example.invalid", "model", "key")
        )
        main = MainModel()
        with patch("urllib.request.urlopen", side_effect=TimeoutError("hidden")):
            result = Engine(self.store, main, extractor=failing).run(self.s["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["requirements_status"]["status"], "fallback")
        self.assertIn(self.text, main.messages[1]["content"])

    def test_original_read_tool_is_bounded_and_session_scoped(self):
        self.prepare()
        tool = Tools(self.store, self.s)
        first = tool.call("read_user_request", {"source": "initial"}, "read")
        self.assertEqual(first["text"], self.text[:3000])
        with self.assertRaises(ValueError):
            tool.call("read_user_request", {"source": "message:99"}, "bad")
        with self.assertRaises(ValueError):
            tool.call("read_user_request", {"source": "../other"}, "bad")

    def test_explicit_conditions_remain_authoritative(self):
        protect_requirements(self.s, constraints=["必须保留字段X"])
        self.prepare()
        context, _ = assemble(self.s, [])
        self.assertIn("必须保留字段X", context[1]["content"])

    def test_engine_accounts_usage_and_persists_view(self):
        main = MainModel()
        e = Engine(self.store, main, extractor=self.extractor)
        result = e.run(self.s["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["usage_tokens"], 123)
        self.assertEqual(
            self.store.get(result["id"])["requirements_view"]["version"], 1
        )
        self.assertNotIn("业务背景说明", json.dumps(main.messages, ensure_ascii=False))

    def test_very_long_input_failure_stops_before_main_model(self):
        main = MainModel()
        self.s["task"] = "原文" * 7000
        self.store.save(self.s)
        self.model.payload = {}
        result = Engine(self.store, main, extractor=self.extractor).run(self.s["id"])
        self.assertEqual(result["status"], "provider_error")
        self.assertFalse(hasattr(main, "messages"))
        self.assertEqual(self.store.get(result["id"])["task"], "原文" * 7000)

    def test_api_long_input_uses_enabled_extractor(self):
        app = create_app(
            self.root,
            self.root / "api",
            token="test",
            provider=MainModel(),
            extractor=self.extractor,
        )
        try:
            with TestClient(app) as c:
                h = {"Authorization": "Bearer test"}
                r = c.post(
                    "/api/sessions",
                    headers=h,
                    json={"task": self.text + "附加背景" * 1200},
                )
                self.assertEqual(r.status_code, 200)
                sid = r.json()["id"]
                c.post(f"/api/sessions/{sid}/run", headers=h)
                s = c.get(f"/api/sessions/{sid}", headers=h).json()
                self.assertEqual(s["requirements_status"]["status"], "extracted")
                self.assertEqual(s["status"], "completed")
        finally:
            app.state.store.close()


class RequirementModelTests(unittest.TestCase):
    def test_json_mode_request_and_response(self):
        body = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": '{"covered_sources": ["initial"], "items": []}'
                    },
                }
            ],
            "usage": {"total_tokens": 9},
        }
        with patch(
            "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())
        ) as request:
            payload, usage = RequirementModel(
                "https://example.invalid/v1", "model", "not-a-real-key"
            ).extract([], [])
            wire = json.loads(request.call_args.args[0].data)
        self.assertEqual(wire["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", wire)
        self.assertEqual(usage["total_tokens"], 9)
        self.assertEqual(payload["covered_sources"], ["initial"])

    def test_timeout_is_bounded_and_sanitized(self):
        with patch(
            "urllib.request.urlopen", side_effect=TimeoutError("private")
        ) as request:
            with self.assertRaises(ExtractionError) as exc:
                RequirementModel("https://example.invalid", "model", "key").extract(
                    [], []
                )
            self.assertNotIn("private", str(exc.exception))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["timeout"], 15)

    def test_truncation_and_bad_json_rejected(self):
        for finish, content in [("length", "{}"), ("stop", "not json")]:
            body = {
                "choices": [{"finish_reason": finish, "message": {"content": content}}]
            }
            with patch(
                "urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(body).encode()),
            ):
                with self.assertRaises(ExtractionError):
                    RequirementModel("https://example.invalid", "model", "key").extract(
                        [], []
                    )

    def test_disabled_by_default_and_separate_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(configured_extractor())
        with patch.dict(
            os.environ,
            {
                "REPO_AGENT_REQUIREMENTS_ENABLED": "1",
                "REPO_AGENT_API_KEY": "main-secret",
            },
            clear=True,
        ):
            extractor = configured_extractor()
            self.assertEqual(extractor.model.key, "")
            with self.assertRaises(ExtractionError):
                extractor.model.extract([], [])
