import asyncio
import hashlib
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .deliberation import Deliberation
from .demo import ScriptedDemo
from .engine import Engine
from .provider import ChatProvider
from .requirements import configured_extractor
from .store import Store
from .task_state import requirement_list


class MemoryPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class NewTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=16000, pattern=r"\S")
    agent_mode: Literal["react", "plan_execute"] = "react"
    learning_enabled: bool = False
    max_steps: int = Field(default=20, ge=1, le=40)
    constraints: list[str] = Field(default_factory=list, max_length=8)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("constraints", "acceptance_criteria")
    @classmethod
    def validate_requirements(cls, values):
        return requirement_list(values)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: bool


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=512)


def create_app(
    root=None,
    state_dir=None,
    *,
    token=None,
    provider=None,
    backend=None,
    extractor=None,
    database_url=None,
):
    root = Path(root or os.getenv("REPO_AGENT_WORKSPACE", ".")).resolve()
    store = Store(
        state_dir or os.getenv("REPO_AGENT_STATE", ".runtime"),
        database_url=database_url or os.getenv("REPO_AGENT_DATABASE_URL"),
    )
    mode = os.getenv("REPO_AGENT_MODE", "live")
    provider = provider or (ScriptedDemo() if mode == "demo" else ChatProvider())
    engine = Engine(
        store,
        provider,
        deliberator=None
        if isinstance(provider, ScriptedDemo)
        else Deliberation(ChatProvider(timeout=15, attempts=1)),
        backend=backend or os.getenv("REPO_AGENT_EXECUTION", "disabled"),
        extractor=extractor
        or (None if isinstance(provider, ScriptedDemo) else configured_extractor()),
    )

    @asynccontextmanager
    async def lifespan(app):
        yield
        store.close()

    app = FastAPI(title="Repo Workbench", version="0.2.0", lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    secret = token or os.getenv("REPO_AGENT_WEB_TOKEN") or secrets.token_urlsafe(32)
    app.state.token = secret
    cookie_name = (
        "repo_session_" + hashlib.sha256(str(store.directory).encode()).hexdigest()[:12]
    )
    cookies = set()
    guard = threading.RLock()
    jobs = {"owner": None, "again": False, "followup": None}

    def same_origin(request: Request):
        origin = request.headers.get("origin")
        expected = str(request.base_url).rstrip("/")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and origin != expected
        ):
            raise HTTPException(403, "仅允许当前工作台页面发起请求")

    def auth(request: Request, authorization: str = Header(default="")):
        same_origin(request)
        cookie = request.cookies.get(cookie_name, "")
        with guard:
            valid_cookie = cookie in cookies
        if not valid_cookie and not secrets.compare_digest(
            authorization, "Bearer " + secret
        ):
            raise HTTPException(401, "请先登录工作台")

    def session(sid):
        try:
            s = store.get(sid)
        except KeyError:
            raise HTTPException(404, "会话不存在") from None
        if s["root"] != str(root):
            raise HTTPException(404, "会话不属于当前工作区")
        return s

    def drain(sid):
        try:
            while True:
                with guard:
                    followup = jobs["followup"]
                    jobs["followup"] = None
                if followup:
                    engine.follow_up(sid, followup, origin="approval_notice")
                engine.run(sid)
                with guard:
                    if jobs["again"]:
                        jobs["again"] = False
                        continue
                    jobs["owner"] = None
                    return
        except Exception:
            with guard:
                jobs.update(owner=None, again=False, followup=None)
            store.event(
                sid, "execution_error", {"message": "执行未完成，请检查会话后重试"}
            )

    def schedule(sid, tasks, resume=False):
        with guard:
            if jobs["owner"] is not None:
                if jobs["owner"] != sid:
                    raise HTTPException(409, "工作区正在执行其他任务，请稍后再试")
                if resume:
                    jobs["again"] = True
                return {"status": "queued" if resume else "already_running"}
            jobs["owner"] = sid
            jobs["again"] = False
            tasks.add_task(drain, sid)
            return {"status": "queued"}

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (
            Path(__file__)
            .with_name("static")
            .joinpath("index.html")
            .read_text(encoding="utf-8")
        )

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        same_origin(request)
        if not secrets.compare_digest(body.password.encode(), secret.encode()):
            raise HTTPException(401, "访问密码不正确，请使用本次启动时的工作台密码")
        value = secrets.token_urlsafe(32)
        with guard:
            old = request.cookies.get(cookie_name)
            cookies.discard(old)
            if len(cookies) >= 100:
                cookies.pop()
            cookies.add(value)
        response.set_cookie(
            cookie_name,
            value,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return {"status": "connected"}

    @app.post("/api/logout", dependencies=[Depends(auth)])
    def logout(request: Request, response: Response):
        with guard:
            cookies.discard(request.cookies.get(cookie_name))
        response.delete_cookie(cookie_name)
        return {"status": "logged_out"}

    @app.get("/api/config", dependencies=[Depends(auth)])
    def config():
        return {
            "workspace": str(root),
            "mode": "offline-scripted"
            if isinstance(provider, ScriptedDemo)
            else "live-model",
            "backend": engine.backend,
            "model": getattr(provider, "model", "scripted-demo"),
            "requirement_extraction": engine.extractor is not None,
            "database": store.backend,
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
        try:
            return store.create(
                root,
                body.task,
                body.max_steps,
                constraints=body.constraints,
                acceptance_criteria=body.acceptance_criteria,
                semantic_requirements=engine.extractor is not None,
                agent_mode=body.agent_mode,
                learning_enabled=body.learning_enabled,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/sessions/{sid}", dependencies=[Depends(auth)])
    def get(sid: str):
        s = session(sid)
        s["events"], s["approvals"] = store.events(sid), store.approvals(sid)
        return s

    @app.get("/api/sessions/{sid}/events", dependencies=[Depends(auth)])
    async def events(sid: str, request: Request, after: int = 0):
        await asyncio.to_thread(session, sid)
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor") from None

        async def stream():
            cursor, signature, ticks = max(0, after), "", 0
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(
                    store.events, sid, after=cursor, limit=200
                )
                for row in rows:
                    cursor = row["id"]
                    yield f"id: {cursor}\nevent: trace\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                s = await asyncio.to_thread(session, sid)
                state = {
                    k: s.get(k)
                    for k in [
                        "status",
                        "steps",
                        "verification",
                        "usage_tokens",
                        "final",
                        "compression_count",
                    ]
                }
                current = json.dumps(state, ensure_ascii=False)
                if current != signature:
                    signature = current
                    yield f"event: state\ndata: {current}\n\n"
                with guard:
                    active = jobs["owner"] == sid
                if (
                    not active
                    and s["status"] not in {"running", "ready"}
                    and len(rows) < 200
                ):
                    yield "event: idle\ndata: {}\n\n"
                    return
                ticks += 1
                if ticks % 40 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{sid}/follow-up", dependencies=[Depends(auth)])
    def follow_up(sid: str, body: NewTask):
        session(sid)
        with guard:
            if jobs["owner"] is not None:
                raise HTTPException(409, "当前工作区仍在执行，请稍后追加")
            try:
                return engine.follow_up(
                    sid,
                    body.task,
                    body.max_steps,
                    constraints=body.constraints,
                    acceptance_criteria=body.acceptance_criteria,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None

    @app.post("/api/sessions/{sid}/stop", dependencies=[Depends(auth)])
    def stop(sid: str):
        session(sid)
        with guard:
            if jobs["owner"] == sid:
                jobs["again"] = False
        return engine.cancel(sid)

    @app.post("/api/sessions/{sid}/run", dependencies=[Depends(auth)])
    def run(sid: str, tasks: BackgroundTasks):
        session(sid)
        return schedule(sid, tasks)

    @app.post("/api/sessions/{sid}/approvals/{aid}", dependencies=[Depends(auth)])
    def approve(sid: str, aid: str, body: Decision, tasks: BackgroundTasks):
        s = session(sid)
        with guard:
            if jobs["owner"] not in {None, sid}:
                raise HTTPException(409, "工作区正在执行其他任务，请稍后审核")
            try:
                store.resolve(sid, aid, body.allow)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            if s["status"] == "waiting_approval" and s.get("pending_approval") == aid:
                result = schedule(sid, tasks, resume=True)
                return {**result, "auto_resumed": True}
            if s["status"] in {"completed", "completed_unverified"}:
                jobs["followup"] = (
                    "用户已"
                    + ("批准" if body.allow else "拒绝")
                    + "操作 "
                    + aid
                    + "。请根据审批结果继续完成原任务；权限边界保持不变。"
                )
                result = schedule(sid, tasks, resume=True)
                return {**result, "auto_resumed": True}
        return {"status": "recorded", "auto_resumed": False}

    @app.post("/api/sessions/{sid}/memory", dependencies=[Depends(auth)])
    def memory_preference(sid: str, body: MemoryPreference):
        with guard:
            s = session(sid)
            if jobs["owner"] is not None or s["status"] == "running":
                raise HTTPException(409, "执行中不能切换经验设置，请等待暂停或结束")
            s["learning_enabled"] = body.enabled
            s["recalled_lessons"] = []
            s["memory_retrieval"] = {"mode": "pending" if body.enabled else "disabled"}
            store.save(s)
            store.event(sid, "memory_preference", {"enabled": body.enabled})
        return {"enabled": body.enabled}

    @app.delete("/api/lessons/{lid}", dependencies=[Depends(auth)])
    def delete_lesson(lid: str):
        return {"deleted": store.delete_lesson(root, lid)}

    return app
