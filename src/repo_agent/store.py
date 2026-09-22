import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(
            self.directory / "sessions.db", check_same_thread=False
        )
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, session TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,session TEXT,kind TEXT,data TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS calls(session TEXT,id TEXT,status TEXT,result TEXT,PRIMARY KEY(session,id));
        CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,session TEXT,kind TEXT,data TEXT,decision TEXT DEFAULT 'pending');
        """)
        self.db.commit()

    def create(self, root, task, max_steps=20):
        s = dict(
            id=uuid.uuid4().hex,
            root=str(Path(root).resolve()),
            task=task,
            status="ready",
            messages=[],
            steps=0,
            max_steps=max_steps,
            usage_tokens=0,
            notes="",
            final="",
            verification="not_run",
            revision=0,
            test_revision=-1,
            compression_count=0,
        )
        self.save(s)
        self.event(s["id"], "created", {"task": task})
        return s

    def save(self, session):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO sessions VALUES(?,?)",
                (session["id"], json.dumps(session, ensure_ascii=False)),
            )

    def get(self, sid):
        with self.lock:
            row = self.db.execute(
                "SELECT data FROM sessions WHERE id=?", (sid,)
            ).fetchone()
        if not row:
            raise KeyError("Unknown session")
        return json.loads(row[0])

    def sessions(self):
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT data FROM sessions ORDER BY rowid DESC LIMIT 100"
                )
            ]

    def event(self, sid, kind, data):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(session,kind,data,created) VALUES(?,?,?,?)",
                (sid, kind, json.dumps(data, ensure_ascii=False), time.time()),
            )

    def events(self, sid):
        with self.lock:
            return [
                dict(r, data=json.loads(r["data"]))
                for r in self.db.execute(
                    "SELECT * FROM events WHERE session=? ORDER BY id", (sid,)
                )
            ]

    def approval(self, sid, kind, data, aid=None):
        aid = aid or uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO approvals(id,session,kind,data) VALUES(?,?,?,?)",
                (aid, sid, kind, json.dumps(data, ensure_ascii=False)),
            )
        return aid

    def approvals(self, sid):
        with self.lock:
            return [
                dict(r, data=json.loads(r["data"]))
                for r in self.db.execute(
                    "SELECT * FROM approvals WHERE session=? ORDER BY rowid", (sid,)
                )
            ]

    def resolve(self, sid, aid, allow):
        with self.lock, self.db:
            n = self.db.execute(
                "UPDATE approvals SET decision=? WHERE id=? AND session=? AND decision='pending'",
                ("allow" if allow else "deny", aid, sid),
            ).rowcount
        if n != 1:
            raise ValueError("Approval missing or already decided")
        self.event(sid, "approval_decided", {"id": aid, "allowed": allow})

    def call(self, sid, cid):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM calls WHERE session=? AND id=?", (sid, cid)
            ).fetchone()
        return dict(row) if row else None

    def put_call(self, sid, cid, status, result=None):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO calls VALUES(?,?,?,?)",
                (sid, cid, status, json.dumps(result, ensure_ascii=False)),
            )

    def artifact(self, text, sid):
        aid = uuid.uuid4().hex
        folder = self.directory / "artifacts"
        folder.mkdir(exist_ok=True)
        (folder / (aid + ".txt")).write_text(text, encoding="utf-8")
        with self.lock, self.db:
            self.db.execute("INSERT INTO artifacts VALUES(?,?)", (aid, sid))
        return aid

    def artifact_owner(self, aid):
        with self.lock:
            row = self.db.execute(
                "SELECT session FROM artifacts WHERE id=?", (aid,)
            ).fetchone()
        return row[0] if row else None

    def read_artifact(self, aid, offset=0):
        if len(aid) != 32 or any(x not in "0123456789abcdef" for x in aid):
            raise ValueError("Invalid artifact ID")
        text = (self.directory / "artifacts" / (aid + ".txt")).read_text(
            encoding="utf-8"
        )
        return {
            "text": text[offset : offset + 4000],
            "next_offset": min(len(text), offset + 4000),
            "total_chars": len(text),
        }
