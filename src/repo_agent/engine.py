import json
import threading
import time

from .context import assemble
from .provider import ProviderError
from .tools import ApprovalNeeded, Tools


class Engine:
    def __init__(
        self,
        store,
        provider,
        *,
        backend="disabled",
        context_budget=18000,
        max_tokens=50000,
        deadline=180,
    ):
        self.store = store
        self.provider = provider
        self.backend = backend
        self.context_budget = context_budget
        self.max_tokens = max_tokens
        self.deadline = deadline
        self.locks = {}
        self.guard = threading.Lock()
        self.cancellations = {}

    def run(self, sid):
        with self.guard:
            root = self.store.get(sid)["root"]
            lock = self.locks.setdefault(root, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("Workspace already running another turn")
        with self.guard:
            self.cancellations[sid] = threading.Event()
        try:
            return self._run(sid)
        finally:
            lock.release()

    def cancel(self, sid):
        with self.guard:
            event = self.cancellations.get(sid)
            if event:
                event.set()
        return {"status": "stop_requested"}

    def follow_up(self, sid, text, max_steps=20):
        if not text.strip() or len(text) > 4000 or not 1 <= max_steps <= 40:
            raise ValueError("Invalid follow-up")
        with self.guard:
            root = self.store.get(sid)["root"]
            lock = self.locks.setdefault(root, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("Workspace is running")
        try:
            s = self.store.get(sid)
            if s["status"] in {"waiting_approval", "running"}:
                raise ValueError("Resolve current turn before adding a follow-up")
            s["messages"].append({"role": "user", "content": text})
            s["status"] = "ready"
            s["final"] = ""
            s["max_steps"] = s["steps"] + max_steps
            self.store.save(s)
            self.store.event(sid, "follow_up", {"message": text})
            return s
        finally:
            lock.release()

    def _run(self, sid):
        s = self.store.get(sid)
        if s["status"] in {
            "completed",
            "completed_unverified",
            "budget_exhausted",
            "stopped",
        }:
            return s
        tools = Tools(self.store, s, backend=self.backend)
        s["status"] = "running"
        self.store.save(s)
        started = time.monotonic()
        try:
            while True:
                if self.cancellations.get(sid, threading.Event()).is_set():
                    s["status"] = "stopped"
                    s["final"] = (
                        "Stopped by operator; pending changes remain inspectable."
                    )
                    break
                # Complete persisted pending calls before requesting another model response.
                assistant = next(
                    (m for m in reversed(s["messages"]) if m["role"] == "assistant"), {}
                )
                calls = assistant.get("tool_calls", [])
                done = {
                    m.get("tool_call_id") for m in s["messages"] if m["role"] == "tool"
                }
                for call in calls:
                    cid = call["id"]
                    if cid in done:
                        continue
                    if time.monotonic() - started > self.deadline:
                        s["status"] = "budget_exhausted"
                        s["final"] = "Turn deadline reached before next tool"
                        self.store.save(s)
                        return s
                    name = call["function"]["name"]
                    prior = self.store.call(sid, cid)
                    if prior and prior["status"] == "done":
                        result = json.loads(prior["result"])
                    elif prior and prior["status"] == "running" and name == "run_tests":
                        result = {
                            "error": "Execution interrupted; previous test process outcome is unknown. Re-check before retrying."
                        }
                    else:
                        self.store.put_call(sid, cid, "running")
                        self.store.event(sid, "tool_start", {"id": cid, "tool": name})
                        try:
                            arguments = json.loads(call["function"]["arguments"])
                            result = tools.call(name, arguments, cid)
                        except ApprovalNeeded as exc:
                            self.store.put_call(sid, cid, "waiting")
                            s["status"] = "waiting_approval"
                            s["pending_approval"] = exc.aid
                            self.store.event(
                                sid, "approval_required", {"id": exc.aid, "tool": name}
                            )
                            self.store.save(s)
                            return s
                        except Exception as exc:
                            result = {
                                "error": str(exc)[:500],
                                "type": type(exc).__name__,
                            }
                        self.store.put_call(sid, cid, "done", result)
                    if name == "apply_edit" and result.get("status") == "applied":
                        s["revision"] += 1
                        s["verification"] = "not_run"
                    if name == "run_tests":
                        s["verification"] = result.get("status", "error")
                        s["test_revision"] = s["revision"]
                    s["messages"].append(
                        {
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    self.store.event(
                        sid, "tool_result", {"id": cid, "tool": name, "result": result}
                    )
                    self.store.save(s)
                    if "error" in result:
                        signature = name + call["function"]["arguments"]
                        s["repeat_errors"] = (
                            s.get("repeat_errors", 0) + 1
                            if s.get("last_error_call") == signature
                            else 1
                        )
                        s["last_error_call"] = signature
                        if s["repeat_errors"] >= 3:
                            s["status"] = "stopped"
                            s["final"] = (
                                "Repeated identical tool failure; operator intervention required."
                            )
                            self.store.save(s)
                            return s
                    else:
                        s["repeat_errors"] = 0
                if (
                    s["steps"] >= s["max_steps"]
                    or s["usage_tokens"] >= self.max_tokens
                    or time.monotonic() - started > self.deadline
                ):
                    s["status"] = "budget_exhausted"
                    s["final"] = (
                        "Execution budget reached. Inspect changes before continuing."
                    )
                    break
                context, cut = assemble(s, tools.skills(), self.context_budget)
                if cut:
                    s["compression_count"] += 1
                self.store.event(
                    sid,
                    "context",
                    {
                        "characters": len(json.dumps(context, ensure_ascii=False)),
                        "omitted_groups": cut,
                    },
                )
                msg, usage = self.provider.complete(context, tools.schemas())
                s["steps"] += 1
                s["usage_tokens"] += int(usage.get("total_tokens", 0) or 0)
                tc = msg.get("tool_calls") or []
                if len(tc) > 8:
                    raise ProviderError("Provider exceeded eight tools per step")
                known = {
                    c["id"] for m in s["messages"] for c in m.get("tool_calls", [])
                }
                validated = []
                for c in tc:
                    if (
                        not isinstance(c.get("id"), str)
                        or not c["id"]
                        or c["id"] in known
                    ):
                        raise ProviderError("Duplicate or invalid tool call ID")
                    f = c.get("function", {})
                    if (
                        not isinstance(f.get("name"), str)
                        or not isinstance(f.get("arguments"), str)
                        or len(f["arguments"]) > 60000
                    ):
                        raise ProviderError("Malformed tool call")
                    known.add(c["id"])
                    validated.append({"id": c["id"], "type": "function", "function": f})
                assistant = {
                    "role": "assistant",
                    "content": str(msg.get("content") or "")[:16000],
                }
                if validated:
                    assistant["tool_calls"] = validated
                s["messages"].append(assistant)
                self.store.save(s)
                if not tc:
                    if not assistant["content"].strip():
                        raise ProviderError(
                            "Empty model response; session retained for retry"
                        )
                    s["status"] = (
                        "completed"
                        if s["revision"] == 0
                        or (
                            s["verification"] == "passed"
                            and s["test_revision"] == s["revision"]
                        )
                        else "completed_unverified"
                    )
                    s["final"] = assistant["content"]
                    break
        except ProviderError as exc:
            s["status"] = "provider_error"
            s["final"] = str(exc)
        except Exception as exc:
            s["status"] = "failed"
            s["final"] = "Runtime failure: " + type(exc).__name__
        self.store.save(s)
        self.store.event(
            sid, "stopped", {"status": s["status"], "verification": s["verification"]}
        )
        return s
