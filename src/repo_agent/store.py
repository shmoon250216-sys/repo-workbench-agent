"""SQLite/MySQL persistence with short transactions and optimistic updates."""

import copy
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, event, insert, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError

from . import schema as t
from .execution import digest
from .task_state import protect_requirements


class StoreConflict(ValueError):
    """Reload before retrying; a stale whole-session save would lose data."""


class Store:
    def __init__(self, directory, database_url=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        url = (
            make_url(database_url)
            if database_url
            else URL.create("sqlite", database=str(self.directory / "sessions-v2.db"))
        )
        if url.drivername not in {"sqlite", "mysql+pymysql"}:
            raise ValueError("Use sqlite or mysql+pymysql for the database URL")
        self.backend = url.get_backend_name()
        kwargs = {"pool_pre_ping": True, "hide_parameters": True}
        if self.backend == "sqlite":
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(
                pool_size=5, max_overflow=5, pool_timeout=10, pool_recycle=1800
            )
            kwargs["connect_args"] = {
                "connect_timeout": 5,
                "read_timeout": 30,
                "write_timeout": 30,
            }
        self.engine = create_engine(url, **kwargs)
        if self.backend == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_settings(dbapi_connection, _):
                dbapi_connection.execute("PRAGMA busy_timeout=30000")

        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).parent / "migrations")
        )
        with self.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        legacy = self.directory / "sessions.db"
        if database_url is None and legacy.exists():
            from .storage_migration import import_legacy

            import_legacy(self, legacy)

    def close(self):
        self.engine.dispose()

    def save_lesson(self, session, data):
        key = digest([session["id"], session["steps"]])
        workspace = digest(str(Path(session["root"]).resolve()))
        with self.transaction() as conn:
            self._lock_session(conn, session["id"])
            conn.execute(delete(t.lessons).where(t.lessons.c.expires < time.time()))
            if not conn.execute(
                select(t.lessons.c.id).where(t.lessons.c.id == key)
            ).first():
                conn.execute(
                    insert(t.lessons).values(
                        id=key,
                        workspace=workspace,
                        session=session["id"],
                        data=data,
                        created=time.time(),
                        expires=time.time() + 30 * 86400,
                    )
                )
            stale = (
                conn.execute(
                    select(t.lessons.c.id)
                    .where(t.lessons.c.workspace == workspace)
                    .order_by(t.lessons.c.created.desc())
                    .offset(100)
                )
                .scalars()
                .all()
            )
            if stale:
                conn.execute(delete(t.lessons).where(t.lessons.c.id.in_(stale)))

    def recall_lessons(self, root, query, exclude=None):
        from .deliberation import terms

        tokens = terms(query)
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(t.lessons)
                    .where(
                        t.lessons.c.workspace == digest(str(Path(root).resolve())),
                        t.lessons.c.expires > time.time(),
                    )
                    .order_by(t.lessons.c.created.desc())
                    .limit(100)
                )
                .mappings()
                .all()
            )
        ranked = []
        for row in rows:
            if row["session"] == exclude:
                continue
            score = len(tokens & terms(json.dumps(row["data"], ensure_ascii=False)))
            if score:
                ranked.append(
                    (
                        score,
                        dict(
                            id=row["id"], source_session=row["session"], **row["data"]
                        ),
                    )
                )
        return [item for _, item in sorted(ranked, key=lambda pair: -pair[0])[:3]]

    def delete_lesson(self, root, lid):
        with self.transaction() as conn:
            return (
                conn.execute(
                    delete(t.lessons).where(
                        t.lessons.c.id == lid,
                        t.lessons.c.workspace == digest(str(Path(root).resolve())),
                    )
                ).rowcount
                == 1
            )

    @contextmanager
    def transaction(self):
        with self.engine.connect() as conn:
            if self.backend == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.begin()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _lock_session(self, conn, sid):
        row = conn.execute(
            select(t.sessions.c.id).where(t.sessions.c.id == sid).with_for_update()
        ).first()
        if row is None:
            raise KeyError("Unknown session")

    def create(
        self,
        root,
        task,
        max_steps=20,
        *,
        constraints=None,
        acceptance_criteria=None,
        semantic_requirements=False,
        agent_mode="react",
        learning_enabled=False,
    ):
        if agent_mode not in {"react", "plan_execute"}:
            raise ValueError("Invalid agent mode")
        s = dict(
            agent_mode=agent_mode,
            learning_enabled=learning_enabled,
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
            semantic_requirements_enabled=semantic_requirements,
        )
        protect_requirements(s, constraints, acceptance_criteria)
        with self.transaction() as conn:
            version = self._save(conn, s)
            self._event(conn, s["id"], "created", {"task": task})
        s["_store_version"] = version
        return s

    def _save(self, conn, session):
        data = {
            k: v for k, v in session.items() if k not in {"messages", "_store_version"}
        }
        now = time.time()
        expected = session.get("_store_version")
        if expected is None:
            version = 1
            try:
                conn.execute(
                    insert(t.sessions).values(
                        id=session["id"],
                        status=session["status"],
                        version=version,
                        data=data,
                        created=now,
                        updated=now,
                    )
                )
            except IntegrityError:
                raise StoreConflict("Session exists; reload before saving") from None
        else:
            version = expected + 1
            changed = conn.execute(
                update(t.sessions)
                .where(
                    t.sessions.c.id == session["id"], t.sessions.c.version == expected
                )
                .values(
                    status=session["status"], version=version, data=data, updated=now
                )
            ).rowcount
            if changed != 1:
                raise StoreConflict(
                    "Session changed concurrently; reload before continuing"
                )
        old = (
            conn.execute(
                select(t.messages.c.data)
                .where(t.messages.c.session == session["id"])
                .order_by(t.messages.c.seq)
            )
            .scalars()
            .all()
        )
        incoming = session["messages"]
        common = 0
        while common < min(len(old), len(incoming)) and old[common] == incoming[common]:
            common += 1
        if common < len(old):
            conn.execute(
                delete(t.messages).where(
                    t.messages.c.session == session["id"], t.messages.c.seq >= common
                )
            )
        if common < len(incoming):
            conn.execute(
                insert(t.messages),
                [
                    dict(session=session["id"], seq=i, data=incoming[i])
                    for i in range(common, len(incoming))
                ],
            )
        return version

    def save(self, session):
        with self.transaction() as conn:
            version = self._save(conn, session)
        session["_store_version"] = version

    def get(self, sid):
        # Brief parent lock keeps state and messages on the same committed version.
        with self.transaction() as conn:
            self._lock_session(conn, sid)
            row = (
                conn.execute(select(t.sessions).where(t.sessions.c.id == sid))
                .mappings()
                .one()
            )
            s = copy.deepcopy(row["data"])
            s["_store_version"] = row["version"]
            s["messages"] = (
                conn.execute(
                    select(t.messages.c.data)
                    .where(t.messages.c.session == sid)
                    .order_by(t.messages.c.seq)
                )
                .scalars()
                .all()
            )
            return s

    def sessions(self):
        with self.engine.connect() as conn:
            ids = (
                conn.execute(
                    select(t.sessions.c.id)
                    .order_by(t.sessions.c.created.desc())
                    .limit(100)
                )
                .scalars()
                .all()
            )
        return [self.get(sid) for sid in ids]

    def _event(self, conn, sid, kind, data):
        conn.execute(
            insert(t.events).values(
                session=sid, kind=kind, data=data, created=time.time()
            )
        )

    def event(self, sid, kind, data):
        with self.transaction() as conn:
            self._event(conn, sid, kind, data)

    def events(self, sid, after=0, limit=100000):
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(t.events)
                    .where(t.events.c.session == sid, t.events.c.id > after)
                    .order_by(t.events.c.id)
                    .limit(limit)
                ).mappings()
            ]

    def approval(self, sid, kind, data, aid=None):
        aid = aid or uuid.uuid4().hex
        with self.transaction() as conn:
            self._lock_session(conn, sid)
            old = (
                conn.execute(select(t.approvals).where(t.approvals.c.id == aid))
                .mappings()
                .first()
            )
            if old:
                if old["session"] != sid or old["kind"] != kind or old["data"] != data:
                    raise ValueError("Approval ID already binds different content")
            else:
                conn.execute(
                    insert(t.approvals).values(
                        id=aid, session=sid, kind=kind, data=data, created=time.time()
                    )
                )
        return aid

    def approvals(self, sid):
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    select(t.approvals)
                    .where(t.approvals.c.session == sid)
                    .order_by(t.approvals.c.created, t.approvals.c.id)
                ).mappings()
            ]

    def ensure_test_approval(self, sid, call_id, spec):
        legacy = hashlib.md5(
            (sid + call_id).encode(), usedforsecurity=False
        ).hexdigest()
        with self.transaction() as conn:
            self._lock_session(conn, sid)
            rows = (
                conn.execute(
                    select(t.approvals)
                    .where(t.approvals.c.session == sid, t.approvals.c.kind == "tests")
                    .order_by(t.approvals.c.created.desc(), t.approvals.c.id.desc())
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            related = [
                r
                for r in rows
                if r["id"] == legacy or r["data"].get("call_id") == call_id
            ]
            if related:
                if any(r["decision"] == "consumed" for r in related):
                    raise ValueError(
                        "This tool call already consumed its test authorization; use a new call ID for a reviewed retry"
                    )
                row = related[0]
                if row["data"].get("spec") == spec and row["decision"] != "stale":
                    return row["id"]
                conn.execute(
                    update(t.approvals)
                    .where(
                        t.approvals.c.id.in_([r["id"] for r in related]),
                        t.approvals.c.decision.in_(["pending", "allow"]),
                    )
                    .values(decision="stale")
                )
            aid = uuid.uuid4().hex
            data = dict(
                call_id=call_id,
                spec=spec,
                spec_sha256=digest(spec),
                runner=spec["runner"],
                backend=spec["backend"],
                command=spec["command"],
                warning="Approval binds this exact execution specification. trusted-local is not a sandbox.",
                supersedes=related[0]["id"] if related else None,
            )
            conn.execute(
                insert(t.approvals).values(
                    id=aid, session=sid, kind="tests", data=data, created=time.time()
                )
            )
        return aid

    def consume_test_approval(self, sid, aid, spec):
        with self.transaction() as conn:
            try:
                self._lock_session(conn, sid)
            except KeyError:
                raise ValueError("Test approval unavailable in this session") from None
            row = (
                conn.execute(
                    select(t.approvals)
                    .where(
                        t.approvals.c.id == aid,
                        t.approvals.c.session == sid,
                        t.approvals.c.kind == "tests",
                        t.approvals.c.decision == "allow",
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            data = row["data"] if row else {}
            if data.get("spec") != spec or data.get("spec_sha256") != digest(spec):
                raise ValueError(
                    "Test approval unavailable, changed, or already consumed"
                )
            changed = conn.execute(
                update(t.approvals)
                .where(
                    t.approvals.c.id == aid,
                    t.approvals.c.session == sid,
                    t.approvals.c.decision == "allow",
                )
                .values(decision="consumed")
            ).rowcount
            if changed != 1:
                raise ValueError(
                    "Test approval was concurrently consumed or invalidated"
                )

    def resolve(self, sid, aid, allow):
        with self.transaction() as conn:
            self._lock_session(conn, sid)
            n = conn.execute(
                update(t.approvals)
                .where(
                    t.approvals.c.id == aid,
                    t.approvals.c.session == sid,
                    t.approvals.c.decision == "pending",
                )
                .values(decision="allow" if allow else "deny")
            ).rowcount
            if n != 1:
                raise ValueError("Approval missing or already decided")
            self._event(conn, sid, "approval_decided", {"id": aid, "allowed": allow})

    def call(self, sid, cid):
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(t.calls).where(t.calls.c.session == sid, t.calls.c.id == cid)
                )
                .mappings()
                .first()
            )
        return (
            dict(row, result=json.dumps(row["result"], ensure_ascii=False))
            if row
            else None
        )

    def _put_call(self, conn, sid, cid, status, result=None):
        where = (t.calls.c.session == sid, t.calls.c.id == cid)
        old = conn.execute(select(t.calls.c.status).where(*where)).scalar_one_or_none()
        if old is None:
            conn.execute(
                insert(t.calls).values(
                    session=sid, id=cid, status=status, result=result
                )
            )
        else:
            if old == "done" and status != "done":
                raise StoreConflict("Completed tool call cannot be restarted")
            conn.execute(
                update(t.calls).where(*where).values(status=status, result=result)
            )

    def put_call(self, sid, cid, status, result=None):
        with self.transaction() as conn:
            self._lock_session(conn, sid)
            self._put_call(conn, sid, cid, status, result)

    def complete_call(self, session, cid, name, result):
        """Commit result, message, notes/revision and event together; no external I/O inside."""
        candidate = copy.deepcopy(session)
        if name == "apply_edit" and result.get("status") == "applied":
            candidate["revision"] += 1
            candidate["verification"] = "not_run"
        if name == "run_tests":
            candidate["verification"] = result.get("status", "error")
            candidate["test_revision"] = candidate["revision"]
            candidate["verification_snapshot"] = result.get("workspace_sha256")
        candidate["messages"].append(
            dict(
                role="tool",
                tool_call_id=cid,
                content=json.dumps(result, ensure_ascii=False),
            )
        )
        with self.transaction() as conn:
            version = self._save(conn, candidate)
            self._put_call(conn, session["id"], cid, "done", result)
            self._event(
                conn,
                session["id"],
                "tool_result",
                dict(id=cid, tool=name, result=result),
            )
        candidate["_store_version"] = version
        session.clear()
        session.update(candidate)

    def wait_for_approval(self, session, cid, name, aid):
        candidate = copy.deepcopy(session)
        candidate.update(status="waiting_approval", pending_approval=aid)
        with self.transaction() as conn:
            version = self._save(conn, candidate)
            self._put_call(conn, session["id"], cid, "waiting")
            self._event(
                conn, session["id"], "approval_required", dict(id=aid, tool=name)
            )
        candidate["_store_version"] = version
        session.clear()
        session.update(candidate)

    def artifact(self, text, sid):
        aid = uuid.uuid4().hex
        with self.transaction() as conn:
            conn.execute(insert(t.artifacts).values(id=aid, session=sid, content=text))
        return aid

    def artifact_owner(self, aid):
        with self.engine.connect() as conn:
            return conn.execute(
                select(t.artifacts.c.session).where(t.artifacts.c.id == aid)
            ).scalar_one_or_none()

    def read_artifact(self, aid, offset=0):
        if len(aid) != 32 or any(x not in "0123456789abcdef" for x in aid):
            raise ValueError("Invalid artifact ID")
        with self.engine.connect() as conn:
            text = conn.execute(
                select(t.artifacts.c.content).where(t.artifacts.c.id == aid)
            ).scalar_one_or_none()
        if text is None:
            raise KeyError("Unknown artifact")
        return dict(
            text=text[offset : offset + 4000],
            next_offset=min(len(text), offset + 4000),
            total_chars=len(text),
        )
