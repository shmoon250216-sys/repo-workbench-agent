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
