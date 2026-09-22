"""Bounded Chat Completions tool-calling adapter; credentials never enter logs."""

import json
import os
import time
import urllib.error
import urllib.request


class ProviderError(RuntimeError):
    pass


class ChatProvider:
    def __init__(self, base_url=None, model=None, api_key=None, timeout=30, attempts=3):
        self.base_url = (base_url or os.getenv("REPO_AGENT_BASE_URL", "")).rstrip("/")
        self.model = model or os.getenv("REPO_AGENT_MODEL", "")
        self.key = api_key or os.getenv("REPO_AGENT_API_KEY", "")
        self.timeout = timeout
        self.attempts = attempts

    def complete(self, messages, tools):
        if not self.base_url or not self.model or not self.key:
            raise ProviderError(
                "Configure REPO_AGENT_BASE_URL, REPO_AGENT_MODEL and REPO_AGENT_API_KEY locally"
            )
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "temperature": 0,
                "max_tokens": 3000,
            },
            ensure_ascii=False,
        ).encode()
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
        )
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read(2_000_000))
                msg = data["choices"][0]["message"]
                if not isinstance(msg, dict):
                    raise ValueError("Invalid message")
                return msg, data.get("usage", {})
            except urllib.error.HTTPError as exc:
                if (
                    exc.code not in {429, 500, 502, 503, 504}
                    or attempt + 1 == self.attempts
                ):
                    raise ProviderError(
                        f"Provider HTTP {exc.code}; response body hidden"
                    ) from None
            except (TimeoutError, urllib.error.URLError):
                if attempt + 1 == self.attempts:
                    raise ProviderError(
                        "Provider network failure after bounded retries"
                    ) from None
            except (KeyError, IndexError, ValueError, TypeError):
                raise ProviderError("Invalid provider response") from None
            time.sleep(min(4, 2**attempt))
        raise ProviderError("Provider unavailable")

    def complete_stream(self, messages, tools, on_delta):
        """Execute only fully assembled tool calls; never retry a partially emitted turn."""
        if not self.base_url or not self.model or not self.key:
            raise ProviderError("Configure model credentials locally")
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "temperature": 0,
                "max_tokens": 3000,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            ensure_ascii=False,
        ).encode()
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + self.key,
                "Content-Type": "application/json",
            },
        )
        for attempt in range(self.attempts):
            emitted = False
            try:
                text, calls, usage, finished = "", {}, {}, False
                total_bytes = 0
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    for raw in response:
                        total_bytes += len(raw)
                        if total_bytes > 2_000_000:
                            raise ProviderError("Model stream exceeded size limit")
                        line = raw.decode("utf-8").strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            finished = True
                            break
                        item = json.loads(payload)
                        if item.get("usage"):
                            usage = item["usage"]
                        for choice in item.get("choices", []):
                            if choice.get("index", 0) != 0:
                                continue
                            delta = choice.get("delta") or {}
                            piece = delta.get("content") or ""
                            if piece:
                                if not isinstance(piece, str):
                                    raise ValueError("Invalid content delta")
                                text += piece
                                emitted = True
                                on_delta(piece)
                            for tool in delta.get("tool_calls") or []:
                                index = tool["index"]
                                if not isinstance(index, int) or not 0 <= index < 8:
                                    raise ValueError("Invalid tool index")
                                acc = calls.setdefault(
                                    index,
                                    {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    },
                                )
                                if tool.get("id"):
                                    acc["id"] += tool["id"]
                                for key in ("name", "arguments"):
                                    acc["function"][key] += (
                                        tool.get("function") or {}
                                    ).get(key) or ""
                                emitted = True
                if not finished:
                    raise ProviderError(
                        "Model stream interrupted; incomplete tools were not executed. Retry explicitly."
                    )
                return {
                    "content": text,
                    "tool_calls": [calls[i] for i in sorted(calls)],
                }, usage
            except urllib.error.HTTPError as exc:
                if (
                    emitted
                    or exc.code not in {429, 500, 502, 503, 504}
                    or attempt + 1 == self.attempts
                ):
                    raise ProviderError(
                        f"Provider HTTP {exc.code}; response body hidden"
                    ) from None
            except (TimeoutError, urllib.error.URLError, ConnectionError):
                if emitted or attempt + 1 == self.attempts:
                    raise ProviderError(
                        "Model stream connection failed; no automatic replay after partial output"
                    ) from None
            except (KeyError, ValueError, TypeError, UnicodeError):
                raise ProviderError("Invalid model stream") from None
            time.sleep(min(4, 2**attempt))
        raise ProviderError("Provider unavailable")
