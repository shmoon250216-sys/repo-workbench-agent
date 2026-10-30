"""Version 1 storage schema. JSON carries domain payloads, columns carry identity/state."""

from alembic import op
from sqlalchemy import (
    JSON,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.mysql import LONGTEXT

metadata = MetaData()
options = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}
sessions = Table(
    "sessions",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("status", String(40), nullable=False),
    Column("version", Integer, nullable=False),
    Column("data", JSON, nullable=False),
    Column("created", Float(precision=53), nullable=False),
    Column("updated", Float(precision=53), nullable=False),
    **options,
)
messages = Table(
    "messages",
    metadata,
    Column("session", String(32), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("data", JSON, nullable=False),
    **options,
)
events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session", String(32), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("data", JSON, nullable=False),
    Column("created", Float(precision=53), nullable=False),
    Index("ix_events_session_id", "session", "id"),
    **options,
)
calls = Table(
    "calls",
    metadata,
    Column("session", String(32), primary_key=True),
    Column("id", String(255), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("result", JSON),
    **options,
)
approvals = Table(
    "approvals",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("session", String(32), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("data", JSON, nullable=False),
    Column("decision", String(32), nullable=False, server_default="pending"),
    Column("created", Float(precision=53), nullable=False),
    Index("ix_approvals_session_kind_created", "session", "kind", "created"),
    **options,
)
artifacts = Table(
    "artifacts",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("session", String(32), nullable=False),
    Column("content", Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
    **options,
)
imports = Table(
    "legacy_imports",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("receipt", JSON, nullable=False),
    **options,
)

# Frozen schema: do not import live application metadata in old revisions.
revision = "0001_storage"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    raise RuntimeError("Restore a database backup instead of destructive downgrade")
