import argparse
import json
import sys
from pathlib import Path

from .demo import ScriptedDemo, create_fixture
from .engine import Engine
from .provider import ChatProvider
from .store import Store


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Repo Workbench: bounded coding agent")
    p.add_argument("--workspace", default=".")
    p.add_argument("--state", default=".runtime")
    p.add_argument(
        "--backend", choices=["disabled", "docker", "trusted-local"], default="disabled"
    )
    p.add_argument(
        "--demo",
        action="store_true",
        help="Use deterministic fixture provider, not a model",
    )
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("task")
    run.add_argument("--max-steps", type=int, default=20)
    resume = sub.add_parser("resume")
    resume.add_argument("session")
    follow = sub.add_parser("follow-up")
    follow.add_argument("session")
    follow.add_argument("task")
    show = sub.add_parser("show")
    show.add_argument("session")
    approve = sub.add_parser("approve")
    approve.add_argument("session")
    approve.add_argument("approval")
    approve.add_argument("--deny", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8022)
    sub.add_parser("init-demo")
    a = p.parse_args()
    if a.command == "init-demo":
        create_fixture(Path(a.workspace))
        print("Offline fixture created")
        return
    if a.command == "serve":
        import uvicorn

        from .api import create_app

        app = create_app(
            a.workspace,
            a.state,
            provider=ScriptedDemo() if a.demo else ChatProvider(),
            backend=a.backend,
        )
        print(
            f"Local UI: http://127.0.0.1:{a.port}/#token={app.state.token}", flush=True
        )
        uvicorn.run(app, host="127.0.0.1", port=a.port)
        return
    store = Store(a.state)
    if a.command == "show":
        print(json.dumps(store.get(a.session), ensure_ascii=False, indent=2))
        return
    if a.command == "approve":
        store.resolve(a.session, a.approval, not a.deny)
        print("Decision saved. Resume explicitly.")
        return
    if a.command == "run":
        if not 1 <= a.max_steps <= 40 or not a.task.strip():
            p.error("max-steps must be 1..40 and task must be nonempty")
        s = store.create(a.workspace, a.task, a.max_steps)
    else:
        s = store.get(a.session)
        if s["root"] != str(Path(a.workspace).resolve()):
            p.error("Resume workspace differs from saved session")
    engine = Engine(
        store, ScriptedDemo() if a.demo else ChatProvider(), backend=a.backend
    )
    if a.command == "follow-up":
        engine.follow_up(s["id"], a.task)
    s = engine.run(s["id"])
    print(
        json.dumps(
            {
                k: s.get(k)
                for k in [
                    "id",
                    "status",
                    "steps",
                    "verification",
                    "final",
                    "pending_approval",
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    for approval in store.approvals(s["id"]):
        if approval["decision"] == "pending":
            print(json.dumps(approval, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
