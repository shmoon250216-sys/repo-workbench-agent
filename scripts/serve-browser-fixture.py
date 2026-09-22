"""Only for browser regression: delayed scripted output, not a real model."""

import argparse
import time
from pathlib import Path

import uvicorn

from repo_agent.api import create_app
from repo_agent.demo import ScriptedDemo, create_fixture


class SlowDemo(ScriptedDemo):
    def complete_stream(self, messages, tools, on_delta):
        msg, usage = self.complete(messages, tools)
        for part in ["正在", "读取", "当前", "代码", "，请", "稍候", "。"]:
            on_delta(part)
            time.sleep(0.15)
        return msg, usage


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, help="New, disposable directory")
    parser.add_argument("--port", type=int, default=8024)
    args = parser.parse_args()
    base = Path(args.directory)
    create_fixture(base / "workspace")
    app = create_app(
        base / "workspace", base / "state", provider=SlowDemo(), backend="trusted-local"
    )
    (base / "ui-token.txt").write_text(app.state.token, encoding="utf-8")
    print("Fixture access password saved locally to", base / "ui-token.txt")
    uvicorn.run(app, host="127.0.0.1", port=args.port)
