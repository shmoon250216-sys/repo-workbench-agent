import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from repo_agent.api import create_app
from repo_agent.deliberation import Deliberation
from repo_agent.demo import ScriptedDemo
from repo_agent.memory_retrieval import HybridMemory, bm25, normalize, tokenize
from repo_agent.store import Store


class FakeEmbedding:
    identity = "fixture-v1"

    def __init__(self):
        self.calls = []

    def embed(self, texts, query=False):
        self.calls.append((texts, query))
        return [
            [1.0, 0.0] if "network" in s or "超时" in s else [0.0, 1.0] for s in texts
        ]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.s = self.store.create(self.root, "network")
        self.fake = FakeEmbedding()
        self.store.memory_retriever = HybridMemory(self.fake)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_chinese_overlapping_tokens(self):
        self.assertEqual(tokenize("连接超时"), ["连接", "接超", "超时"])

    def test_rare_term_gets_more_weight(self):
        scores = bm25("parser fix", ["parser fix", "fix", "fix", "fix"])
        self.assertGreater(scores[0], scores[1])

    def test_semantic_only_hit_and_persistent_cache(self):
        self.store.save_lesson(self.s, {"summary": "超时"})
        first = self.store.recall_lessons(self.root, "network")
        self.assertEqual(len(first), 1)
        self.assertNotIn("_embedding", first[0])
        other = Store(self.root / "state")
        other.memory_retriever = HybridMemory(self.fake)
        try:
            other.recall_lessons(self.root, "network")
            self.assertEqual(
                len(self.fake.calls), 3
            )  # query+documents, then query only after reconnect
        finally:
            other.close()

    def test_model_version_invalidates_cache(self):
        self.store.save_lesson(self.s, {"summary": "network"})
        self.store.recall_lessons(self.root, "network")
        self.fake.identity = "fixture-v2"
        self.store.recall_lessons(self.root, "network")
        self.assertEqual(len(self.fake.calls), 4)

    def test_embedding_failure_falls_back_to_bm25(self):
        self.fake.embed = Mock(side_effect=ValueError("bad vector"))
        self.store.save_lesson(self.s, {"summary": "network"})
        self.assertEqual(len(self.store.recall_lessons(self.root, "network")), 1)
        self.assertEqual(self.store.last_memory_retrieval["mode"], "bm25")

    def test_no_candidates_does_not_embed(self):
        self.store.recall_lessons(self.root, "network")
        self.assertEqual(self.fake.calls, [])

    def test_disabled_skips_retrieval_reflection_and_writes(self):
        model = Mock()
        store = Mock()
        s = dict(self.s, recalled_lessons=[{"summary": "old"}])
        Deliberation(model).prepare(s, store)
        Deliberation(model).reflect(s, store)
        store.recall_lessons.assert_not_called()
        store.save_lesson.assert_not_called()
        model.complete.assert_not_called()
        self.assertEqual(s["recalled_lessons"], [])

    def test_invalid_vectors_rejected(self):
        for v in [[], [0, 0], [float("nan"), 1], [float("inf"), 1], [True, 0]]:
            with self.assertRaises(ValueError):
                normalize(v)

    def test_preference_api_auth_and_default(self):
        app = create_app(self.root, self.root / "api", provider=ScriptedDemo())
        with TestClient(app) as c:
            h = {"Authorization": "Bearer " + app.state.token}
            s = c.post("/api/sessions", headers=h, json={"task": "read only"}).json()
            self.assertFalse(s["learning_enabled"])
            url = "/api/sessions/" + s["id"] + "/memory"
            self.assertIn(c.post(url, json={"enabled": True}).status_code, [401, 403])
            self.assertEqual(
                c.post(url, headers=h, json={"enabled": True}).status_code, 200
            )
            self.assertEqual(
                c.post(url, headers=h, json={"enabled": False}).status_code, 200
            )
            self.assertEqual(
                c.post(url, headers=h, json={"enabled": "false"}).status_code, 422
            )
            state = app.state.store.get(s["id"])
            state["status"] = "running"
            app.state.store.save(state)
            self.assertEqual(
                c.post(url, headers=h, json={"enabled": True}).status_code, 409
            )
            state = app.state.store.get(s["id"])
            state["status"] = "waiting_approval"
            app.state.store.save(state)
            self.assertEqual(
                c.post(url, headers=h, json={"enabled": False}).status_code, 200
            )
