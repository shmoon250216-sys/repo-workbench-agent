import os
import secrets
from pathlib import Path
from fastapi import FastAPI, Depends, Header, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from .store import Store
from .engine import Engine
from .provider import ChatProvider
from .demo import ScriptedDemo


class NewTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    max_steps: int = Field(default=20, ge=1, le=40)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: bool


def create_app(root=None, state_dir=None, *, token=None, provider=None, backend=None):
    root = Path(root or os.getenv("REPO_AGENT_WORKSPACE", ".")).resolve()
    state_dir = Path(state_dir or os.getenv("REPO_AGENT_STATE", ".runtime"))
    store = Store(state_dir)
    mode = os.getenv("REPO_AGENT_MODE", "live")
    provider = provider or (ScriptedDemo() if mode == "demo" else ChatProvider())
    engine = Engine(
        store,
        provider,
        backend=backend or os.getenv("REPO_AGENT_EXECUTION", "disabled"),
    )
    app = FastAPI(title="Repo Workbench", version="0.1.0")
    app.state.store = store
    app.state.engine = engine
    secret = token or os.getenv("REPO_AGENT_WEB_TOKEN") or secrets.token_urlsafe(32)
    app.state.token = secret

    def auth(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization, "Bearer " + secret):
            raise HTTPException(401, "请输入本地启动凭据")

    def session(sid):
        try:
            s = store.get(sid)
        except KeyError:
            raise HTTPException(404, "会话不存在") from None
        if s["root"] != str(root):
            raise HTTPException(404, "会话不属于当前工作区")
        return s

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (
            Path(__file__)
            .with_name("static")
            .joinpath("index.html")
            .read_text(encoding="utf-8")
        )

    @app.get("/api/config", dependencies=[Depends(auth)])
    def config():
        return {
            "workspace": str(root),
            "mode": "offline-scripted"
            if isinstance(provider, ScriptedDemo)
            else "live-model",
            "backend": engine.backend,
            "model": getattr(provider, "model", "scripted-demo"),
        }

    @app.get("/api/sessions", dependencies=[Depends(auth)])
    def sessions():
        return [
            {k: s[k] for k in ["id", "task", "status", "steps", "verification"]}
            for s in store.sessions()
            if s["root"] == str(root)
        ]

    @app.post("/api/sessions", dependencies=[Depends(auth)])
    def create(body: NewTask):
        return store.create(root, body.task, body.max_steps)

    @app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
    def get(sid: str):
        s = session(sid)
        s["events"] = store.events(sid)
        s["approvals"] = store.approvals(sid)
        return s

    @app.post("/api/sessions/{sid}/follow-up", dependencies=[Depends(auth)])
    def follow_up(sid: str, body: NewTask):
        session(sid)
        try:
            return engine.follow_up(sid, body.task, body.max_steps)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/sessions/{sid}/stop", dependencies=[Depends(auth)])
    def stop(sid: str):
        session(sid)
        return engine.cancel(sid)

    @app.post("/api/sessions/{sid}/run", dependencies=[Depends(auth)])
    def run(sid: str, tasks: BackgroundTasks):
        session(sid)
        tasks.add_task(engine.run, sid)
        return {"status": "queued"}

    @app.post("/api/sessions/{sid}/approvals/{aid}", dependencies=[Depends(auth)])
    def approve(sid: str, aid: str, body: Decision):
        session(sid)
        try:
            store.resolve(sid, aid, body.allow)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"status": "recorded"}

    return app
