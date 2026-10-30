import sqlalchemy as sa
from alembic import op

revision = "0002_lessons"
down_revision = "0001_storage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lessons",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace", sa.String(64), nullable=False),
        sa.Column("session", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created", sa.Float(precision=53), nullable=False),
        sa.Column("expires", sa.Float(precision=53), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_bin",
    )
    op.create_index("ix_lessons_workspace_created", "lessons", ["workspace", "created"])


def downgrade():
    raise RuntimeError("Restore backup rather than deleting experience records")
