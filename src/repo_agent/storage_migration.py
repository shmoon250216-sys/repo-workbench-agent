"""Offline, non-destructive import of the original SQLite database and artifacts."""

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from sqlalchemy import insert, select

from . import schema as t


def import_legacy(store, source):
    source = Path(source).resolve(strict=True)
    source_id = hashlib.sha256(str(source).encode()).hexdigest()
    with store.engine.connect() as conn:
        prior = conn.execute(
            select(t.imports.c.receipt).where(t.imports.c.source == source_id)
        ).scalar_one_or_none()
    if prior:
        return {**prior, "already_imported": True}
    backup_dir = store.directory / "migration-backups"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / (uuid.uuid4().hex + ".db")
    # Backup API gives a consistent DB image, including WAL; source remains read-only.
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(backup)) as dst,
    ):
        src.backup(dst)
    with closing(sqlite3.connect(backup)) as db:
        db.row_factory = sqlite3.Row
        rows = {
            name: [dict(r) for r in db.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in ("sessions", "calls", "approvals", "events", "artifacts")
        }
    # Read external artifacts before touching target tables. Missing files abort import.
    for row in rows["artifacts"]:
        aid = row["id"]
        if len(aid) != 32 or any(c not in "0123456789abcdef" for c in aid):
            raise ValueError("Invalid legacy artifact ID")
        row["content"] = (source.parent / "artifacts" / (aid + ".txt")).read_text(
            encoding="utf-8"
        )
    receipt = dict(
        counts={name: len(data) for name, data in rows.items()},
        backup=backup.name,
        imported_at=time.time(),
        already_imported=False,
    )
    with store.transaction() as conn:
        # A duplicate session raises StoreConflict: no silent overwrite of live state.
        for row in rows["sessions"]:
            session = json.loads(row["data"])
            session.pop("_store_version", None)
            store._save(conn, session)
        for row in rows["calls"]:
            conn.execute(
                insert(t.calls).values(
                    **{
                        **row,
                        "result": json.loads(row["result"]) if row["result"] else None,
                    }
                )
            )
        for index, row in enumerate(rows["approvals"]):
            conn.execute(
                insert(t.approvals).values(
                    **{
                        **row,
                        "data": json.loads(row["data"]),
                        "created": receipt["imported_at"] + index * 0.00001,
                    }
                )
            )
        for row in rows["events"]:
            # Event IDs are local to each source database; allocate new global IDs.
            conn.execute(
                insert(t.events).values(
                    session=row["session"],
                    kind=row["kind"],
                    data=json.loads(row["data"]),
                    created=row["created"],
                )
            )
        for row in rows["artifacts"]:
            conn.execute(insert(t.artifacts).values(**row))
        conn.execute(insert(t.imports).values(source=source_id, receipt=receipt))
    return receipt
