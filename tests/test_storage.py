"""Same persistence contract against SQLite and an opt-in real MySQL database."""

import concurrent.futures
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url

from repo_agent import schema as t
from repo_agent.demo import ScriptedDemo, create_fixture
from repo_agent.engine import Engine
from repo_agent.storage_migration import import_legacy
from repo_agent.store import Store, StoreConflict


class StorageContract:
    database_url = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.handles = []
        self.store = self.open_store()
        self.s = self.store.create(self.base, "修复计算，保留接口 🧪")

    def open_store(self):
        store = Store(self.base / "state", database_url=self.database_url)
        self.handles.append(store)
        return store

    def tearDown(self):
        for store in self.handles:
            store.close()
        self.tmp.cleanup()

    def spec(self):
        return dict(
            runner="unittest",
            backend="trusted-local",
            command=["python", "-I", "-m", "unittest"],
            snapshot={"sha256": "example"},
            session_id=self.s["id"],
            call_id="c1",
        )

    def parallel(self, fn):
        other = self.open_store()
        barrier = threading.Barrier(2)

        def invoke(store):
            barrier.wait(timeout=10)
            return fn(store)

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(invoke, store) for store in (self.store, other)]
            return [f.result(timeout=30) for f in futures]

    def test_schema_indexes_and_message_separation(self):
        self.s["messages"].append(dict(role="user", content="补充要求"))
        self.store.save(self.s)
        with self.store.engine.connect() as c:
            row = (
                c.execute(select(t.sessions).where(t.sessions.c.id == self.s["id"]))
                .mappings()
                .one()
            )
            self.assertNotIn("messages", row["data"])
            self.assertEqual(
                c.execute(
                    select(t.messages.c.data).where(
                        t.messages.c.session == self.s["id"]
                    )
                ).scalar()["content"],
                "补充要求",
            )
        self.assertIn(
            "ix_events_session_id",
            {i["name"] for i in inspect(self.store.engine).get_indexes("events")},
        )

    def test_unicode_artifact_survives_reopen_without_local_file(self):
        text = "中文🧪" * 2000
        aid = self.store.artifact(text, self.s["id"])
        other = self.open_store()
        self.assertEqual(other.get(self.s["id"])["task"], self.s["task"])
        self.assertEqual(other.read_artifact(aid)["text"], text[:4000])
        self.assertFalse((self.store.directory / "artifacts" / (aid + ".txt")).exists())

    def test_stale_save_does_not_overwrite_messages(self):
        stale = self.store.get(self.s["id"])
        self.s["messages"].append(dict(role="user", content="新增需求"))
        self.store.save(self.s)
        stale["notes"] = "stale"
        with self.assertRaises(StoreConflict):
            self.store.save(stale)
        self.assertEqual(len(self.store.get(self.s["id"])["messages"]), 1)

    def test_concurrent_session_update_one_winner(self):
        def save(store):
            candidate = dict(self.s, notes=uuid.uuid4().hex)
            try:
                store.save(candidate)
                return True
            except StoreConflict:
                return False

        self.assertEqual(sum(self.parallel(save)), 1)

    def test_concurrent_approval_creation_one_record(self):
        ids = self.parallel(
            lambda store: store.ensure_test_approval(self.s["id"], "c1", self.spec())
        )
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(self.store.approvals(self.s["id"])), 1)

    def test_concurrent_claim_one_winner(self):
        aid = self.store.ensure_test_approval(self.s["id"], "c1", self.spec())
        self.store.resolve(self.s["id"], aid, True)

        def claim(store):
            try:
                store.consume_test_approval(self.s["id"], aid, self.spec())
                return True
            except ValueError:
                return False

        self.assertEqual(sum(self.parallel(claim)), 1)
        self.assertEqual(self.store.approvals(self.s["id"])[0]["decision"], "consumed")

    def test_approval_and_audit_event_rollback_together(self):
        aid = self.store.approval(self.s["id"], "edit", {"diff": "example"})
        with patch.object(
            self.store, "_event", side_effect=RuntimeError("injected failure")
        ):
            with self.assertRaises(RuntimeError):
                self.store.resolve(self.s["id"], aid, True)
        self.assertEqual(self.store.approvals(self.s["id"])[0]["decision"], "pending")

    def test_call_result_message_notes_rollback_together(self):
        self.store.put_call(self.s["id"], "c1", "running")
        self.s["notes"] = "tool wrote these notes in memory"
        old_version = self.s["_store_version"]
        with patch.object(
            self.store, "_event", side_effect=RuntimeError("injected failure")
        ):
            with self.assertRaises(RuntimeError):
                self.store.complete_call(
                    self.s, "c1", "save_notes", {"status": "saved"}
                )
        self.assertEqual(self.s["_store_version"], old_version)
        loaded = self.open_store().get(self.s["id"])
        self.assertEqual(loaded["notes"], "")
        self.assertEqual(loaded["messages"], [])
        self.assertEqual(self.store.call(self.s["id"], "c1")["status"], "running")
        self.store.complete_call(self.s, "c1", "save_notes", {"status": "saved"})
        loaded = self.open_store().get(self.s["id"])
        self.assertEqual(loaded["notes"], self.s["notes"])
        self.assertEqual(len(loaded["messages"]), 1)
        self.assertEqual(self.store.call(self.s["id"], "c1")["status"], "done")

    def test_waiting_state_and_tool_record_rollback_together(self):
        with patch.object(
            self.store, "_event", side_effect=RuntimeError("injected failure")
        ):
            with self.assertRaises(RuntimeError):
                self.store.wait_for_approval(self.s, "c1", "apply_edit", "a1")
        self.assertEqual(self.store.get(self.s["id"])["status"], "ready")
        self.assertIsNone(self.store.call(self.s["id"], "c1"))

    def test_reopen_approval_resume_real_fixture(self):
        root = self.base / "workspace"
        create_fixture(root)
        s = self.store.create(root, "Fix addition")
        s = Engine(self.store, ScriptedDemo(), backend="trusted-local").run(s["id"])
        self.assertEqual(s["status"], "waiting_approval")
        for _ in range(3):
            other = self.open_store()
            other.resolve(s["id"], s["pending_approval"], True)
            s = Engine(other, ScriptedDemo(), backend="trusted-local").run(s["id"])
            if s["status"] != "waiting_approval":
                break
        self.assertEqual(s["status"], "completed")
        self.assertEqual(s["verification"], "passed")

    def legacy_fixture(self, missing_artifact=False, collision=False):
        folder = self.base / "legacy"
        folder.mkdir()
        path = folder / "sessions.db"
        s = dict(
            self.s,
            id=self.s["id"] if collision else uuid.uuid4().hex,
            messages=[dict(role="user", content="旧的需求")],
        )
        s.pop("_store_version")
        aid = uuid.uuid4().hex
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY,session TEXT,kind TEXT,data TEXT,created REAL);
            CREATE TABLE calls(session TEXT,id TEXT,status TEXT,result TEXT);
            CREATE TABLE approvals(id TEXT,session TEXT,kind TEXT,data TEXT,decision TEXT);
            CREATE TABLE artifacts(id TEXT,session TEXT);
            """)
            db.execute("INSERT INTO sessions VALUES(?,?)", (s["id"], json.dumps(s)))
            db.execute(
                "INSERT INTO events VALUES(1,?,?,?,1)", (s["id"], "created", "{}")
            )
            db.execute("INSERT INTO artifacts VALUES(?,?)", (aid, s["id"]))
            db.commit()
        if not missing_artifact:
            (folder / "artifacts").mkdir()
            (folder / "artifacts" / (aid + ".txt")).write_text(
                "原始工具结果", encoding="utf-8"
            )
        return path, s["id"], aid

    def test_legacy_import_and_idempotent_receipt(self):
        path, sid, aid = self.legacy_fixture()
        before = path.read_bytes()
        first = import_legacy(self.store, path)
        self.assertEqual(first["counts"]["sessions"], 1)
        self.assertTrue(import_legacy(self.store, path)["already_imported"])
        self.assertEqual(self.store.get(sid)["messages"][0]["content"], "旧的需求")
        self.assertEqual(self.store.read_artifact(aid)["text"], "原始工具结果")
        self.assertEqual(path.read_bytes(), before)

    def test_legacy_missing_artifact_aborts_before_import(self):
        path, sid, _ = self.legacy_fixture(missing_artifact=True)
        with self.assertRaises(FileNotFoundError):
            import_legacy(self.store, path)
        with self.assertRaises(KeyError):
            self.store.get(sid)

    def test_legacy_conflict_does_not_overwrite(self):
        path, _, _ = self.legacy_fixture(collision=True)
        with self.assertRaises(StoreConflict):
            import_legacy(self.store, path)
        self.assertEqual(self.store.get(self.s["id"])["messages"], [])


class SQLiteStorageTests(StorageContract, unittest.TestCase):
    pass


MYSQL_URL = os.getenv("REPO_AGENT_TEST_DATABASE_URL")
if MYSQL_URL and not (make_url(MYSQL_URL).database or "").endswith("_test"):
    raise RuntimeError("Integration database name must end in _test")


@unittest.skipUnless(
    MYSQL_URL, "Set REPO_AGENT_TEST_DATABASE_URL for real MySQL integration"
)
class MySQLStorageTests(StorageContract, unittest.TestCase):
    database_url = MYSQL_URL
