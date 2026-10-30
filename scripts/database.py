"""Initialize/inspect storage or import a stopped legacy SQLite instance. No secrets printed."""

import argparse
import json
import os

from sqlalchemy import func, select

from repo_agent import schema
from repo_agent.storage_migration import import_legacy
from repo_agent.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=".runtime/mysql-ui")
    parser.add_argument(
        "--import-sqlite", help="Stopped legacy sessions.db; source stays untouched"
    )
    args = parser.parse_args()
    store = Store(args.state, database_url=os.getenv("REPO_AGENT_DATABASE_URL"))
    try:
        if args.import_sqlite:
            print(
                json.dumps(import_legacy(store, args.import_sqlite), ensure_ascii=False)
            )
        with store.engine.connect() as conn:
            counts = {
                name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for name, table in schema.metadata.tables.items()
            }
        print(
            json.dumps({"backend": store.backend, "counts": counts}, ensure_ascii=False)
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
