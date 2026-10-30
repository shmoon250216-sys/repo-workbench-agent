import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import update

from repo_agent import schema
from repo_agent.deliberation import Deliberation, record_progress
from repo_agent.engine import Engine
from repo_agent.store import Store


class Fake:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def complete(self, messages, tools):
        self.calls += 1
        return {"content": json.dumps(self.result)}, {"total_tokens": 10}


class DeliberationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.store = Store(Path(self.tmp.name) / "state")
        self.s = self.store.create(
            self.root,
            "Fix parser edge cases",
            agent_mode="plan_execute",
            learning_enabled=True,
        )
        self.model = Fake(
            {
                "steps": [
                    {"goal": "Inspect parser", "acceptance": "Locate bug"},
                    {"goal": "Fix and verify", "acceptance": "Tests pass"},
                ]
            }
        )
        self.d = Deliberation(self.model)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def lesson(self):
        return dict(
            summary="Parser fixed",
            failure="Empty parser input",
            remedy="Check empty input",
            applicability="Parser edge cases",
            evidence_call_ids=["test"],
        )

    def evidence(self, status="passed"):
        self.store.put_call(self.s["id"], "test", "done", {"status": status})
        self.s["messages"].append(
            {
                "role": "tool",
                "tool_call_id": "test",
                "content": json.dumps({"status": status}),
            }
        )

    def test_plan_cached_across_resume(self):
        self.d.prepare(self.s, self.store)
        self.store.save(self.s)
        self.d.prepare(self.store.get(self.s["id"]), self.store)
        self.assertEqual(self.model.calls, 1)

    def test_followup_replans(self):
        self.d.prepare(self.s, self.store)
        self.s["messages"].append({"role": "user", "content": "Also support tabs"})
        self.d.prepare(self.s, self.store)
        self.assertEqual(self.model.calls, 2)

    def test_plan_does_not_skip_steps(self):
        self.d.prepare(self.s, self.store)
        self.evidence()
        with self.assertRaises(ValueError):
            record_progress(
                self.s,
                self.store,
                step_id="p2",
                status="done",
                reason="done",
                evidence_call_ids=["test"],
            )
        record_progress(
            self.s,
            self.store,
            step_id="p1",
            status="done",
            reason="checked",
            evidence_call_ids=["test"],
        )
        self.assertEqual(self.s["plan"]["steps"][0]["status"], "done")

    def test_foreign_or_failed_evidence_rejected(self):
        self.d.prepare(self.s, self.store)
        self.evidence("failed")
        for cid in ("foreign", "test"):
            with self.assertRaises(ValueError):
                record_progress(
                    self.s,
                    self.store,
                    step_id="p1",
                    status="done",
                    reason="done",
                    evidence_call_ids=[cid],
                )

    def test_public_tool_resolves_short_reference(self):
        from repo_agent.tools import Tools

        self.d.prepare(self.s, self.store)
        self.evidence()
        result = Tools(self.store, self.s).call(
            "record_plan_progress",
            dict(
                step_id="p1",
                status="done",
                reason="inspected",
                evidence_call_ids=["e1"],
            ),
            "progress",
        )
        self.assertEqual(result["status"], "done")

    def test_successful_reflection_recalled_in_same_workspace(self):
        self.evidence()
        self.s.update(status="completed", verification="passed", steps=4)
        Deliberation(Fake(self.lesson())).reflect(self.s, self.store)
        self.assertEqual(len(self.store.recall_lessons(self.root, "parser")), 1)
        self.assertEqual(self.store.recall_lessons(self.root / "other", "parser"), [])
        self.assertEqual(
            self.store.recall_lessons(self.root, "parser", exclude=self.s["id"]), []
        )

    def test_failed_reflection_is_session_only(self):
        self.evidence("failed")
        self.s.update(status="completed_unverified", verification="failed", steps=4)
        Deliberation(Fake(self.lesson())).reflect(self.s, self.store)
        self.assertEqual(self.store.recall_lessons(self.root, "parser"), [])
        self.assertEqual(self.s["reflection"]["promotion"], "session_only_unverified")

    def test_unknown_reflection_citation_rejected(self):
        self.evidence()
        item = self.lesson()
        item["evidence_call_ids"] = ["invented"]
        with self.assertRaises(ValueError):
            Deliberation(Fake(item)).reflect(self.s, self.store)

    def test_expiry_and_delete(self):
        self.store.save_lesson(self.s, self.lesson())
        lid = self.store.recall_lessons(self.root, "parser")[0]["id"]
        self.assertFalse(self.store.delete_lesson(self.root / "other", lid))
        with self.store.transaction() as c:
            c.execute(update(schema.lessons).values(expires=0))
        self.assertEqual(self.store.recall_lessons(self.root, "parser"), [])
        self.assertTrue(self.store.delete_lesson(self.root, lid))

    def test_cancel_stops_remaining_batch(self):
        s = self.store.create(self.root, "Read files")

        class Provider:
            def complete(inner, messages, tools):
                return {
                    "tool_calls": [
                        {
                            "id": str(i),
                            "function": {"name": "list_files", "arguments": "{}"},
                        }
                        for i in (1, 2)
                    ]
                }, {}

        e = Engine(self.store, Provider())
        seen = []

        def call(tool, name, args, cid):
            seen.append(cid)
            e.cancel(s["id"])
            return {"files": []}

        with patch("repo_agent.tools.Tools.call", call):
            end = e.run(s["id"])
        self.assertEqual(seen, ["1"])
        self.assertEqual(end["status"], "stopped")

    def test_unfinished_plan_cannot_be_completed(self):
        e = Engine(self.store, Fake("done"), deliberator=self.d)
        s = e.run(self.s["id"])
        self.assertEqual(s["status"], "completed_unverified")
