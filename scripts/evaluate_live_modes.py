"""Real-model paired evaluation on fixed synthetic repos; never fabricated provider output.

Auto-approval is confined to these generated disposable repositories and logic.py edits.
Run with local model environment configured. No credentials or private repository data exported.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

from repo_agent.deliberation import Deliberation
from repo_agent.engine import Engine
from repo_agent.execution import digest, execution_environment
from repo_agent.provider import ChatProvider
from repo_agent.store import Store

CASES = [
    dict(
        id="parser",
        task="修复 parse_ids：逗号分隔整数，忽略首尾空格及空项，保持顺序和重复值。只改logic.py，不改测试或函数签名。运行测试。",
        code='def parse_ids(text):\n    return [int(x) for x in text.split(",")]\n',
        checks=[
            'assert m.parse_ids("1,2") == [1,2]',
            'assert m.parse_ids(" ,1,, 2,") == [1,2]',
        ],
        hidden=[
            'assert m.parse_ids("") == []',
            'assert m.parse_ids("-1,0,-1") == [-1,0,-1]',
        ],
    ),
    dict(
        id="pagination",
        task="修复 paginate 并新增 page_info：page从1开始，size须为正整数，否则ValueError；paginate返回当前页列表，page_info返回字典items、total、has_next。超出页码返回空列表。只改logic.py，不改测试。分步核验两个函数并运行测试。",
        code="def paginate(items, page, size):\n    return items[page*size:(page+1)*size]\n\ndef page_info(items, page, size):\n    return {}\n",
        checks=[
            "assert m.paginate([1,2,3],1,2)==[1,2]",
            'assert m.page_info([1,2,3],1,2)=={"items":[1,2],"total":3,"has_next":True}',
        ],
        hidden=[
            'assert m.page_info([],1,2)=={"items":[],"total":0,"has_next":False}',
            "assert m.paginate([1],3,2)==[]",
            'try:\n    m.paginate([1],0,2)\nexcept ValueError:\n    pass\nelse:\n    raise AssertionError("invalid page")',
        ],
    ),
    dict(
        id="retry",
        task="修复 should_retry 与 retry_delay：仅429、500、502、503、504可重试；attempt从0计数，仅attempt<max_attempts-1可重试。delay=min(cap,base*2**attempt)。负attempt及非正max_attempts应ValueError。只改logic.py，不改测试，运行测试。",
        code="def should_retry(status, attempt, max_attempts):\n    return status >= 400\n\ndef retry_delay(attempt, base=1, cap=8):\n    return base\n",
        checks=[
            "assert not m.should_retry(401,0,3)",
            "assert m.should_retry(429,0,3)",
            "assert m.retry_delay(3)==8",
        ],
        hidden=[
            "assert not m.should_retry(500,2,3)",
            "assert not m.should_retry(501,0,3)",
            "assert m.retry_delay(5,2,7)==7",
        ],
    ),
    dict(
        id="inventory",
        task="修复 merge_stock：按sku累加qty并保留首次出现顺序，不能修改输入。新增 available_skus 只返回合并后数量>0的sku。空输入返回空列表，允许负数量表示扣减。只改logic.py，不改测试，检查两个函数并运行测试。",
        code='def merge_stock(rows):\n    return rows\n\ndef available_skus(rows):\n    return [r["sku"] for r in rows]\n',
        checks=[
            'assert m.merge_stock([{"sku":"a","qty":1},{"sku":"a","qty":2}])==[{"sku":"a","qty":3}]',
            'assert m.available_skus([{"sku":"a","qty":0}])==[]',
        ],
        hidden=[
            "assert m.merge_stock([])==[]",
            'x=[{"sku":"b","qty":2},{"sku":"a","qty":1},{"sku":"b","qty":-3}]; assert m.available_skus(x)==["a"]; assert x[0]["qty"]==2',
        ],
    ),
]


def run_case(case, mode, memory=False, shared=None):
    with nullcontext(shared) if shared else tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        root = base / "repo"
        root.mkdir(exist_ok=True)
        (root / "tests").mkdir(exist_ok=True)
        (root / "logic.py").write_text(case["code"], encoding="utf-8")
        header = "import importlib.util\nfrom pathlib import Path\nimport unittest\nspec=importlib.util.spec_from_file_location('logic',Path(__file__).resolve().parents[1]/'logic.py')\nm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
        test = (
            header
            + "class Checks(unittest.TestCase):\n    def test_contract(self):\n"
            + "".join("        " + c + "\n" for c in case["checks"])
        )
        (root / "tests/test_logic.py").write_text(test, encoding="utf-8")
        store = Store(base / "state")
        provider = ChatProvider(timeout=30, attempts=1)
        s = store.create(
            root, case["task"], max_steps=30, agent_mode=mode, learning_enabled=memory
        )
        engine = Engine(
            store,
            provider,
            backend="trusted-local",
            max_tokens=100000,
            deadline=150,
            deliberator=Deliberation(ChatProvider(timeout=20, attempts=1)),
        )
        started = time.monotonic()
        decisions = 0
        try:
            for _ in range(12):
                s = engine.run(s["id"])
                if (
                    s["status"] != "waiting_approval"
                    or time.monotonic() - started > 240
                ):
                    break
                a = next(
                    a
                    for a in store.approvals(s["id"])
                    if a["id"] == s["pending_approval"]
                )
                allow = (a["kind"] == "edit" and a["data"]["path"] == "logic.py") or (
                    a["kind"] == "tests"
                    and a["data"]["spec"]["workspace"] == str(root.resolve())
                )
                store.resolve(s["id"], a["id"], allow)
                decisions += 1
            hidden = base / "holdout.py"
            hidden.write_text(
                "import importlib.util\nspec=importlib.util.spec_from_file_location('logic',"
                + repr(str(root / "logic.py"))
                + ")\nm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n"
                + "\n".join(case["checks"] + case["hidden"])
                + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-I", str(hidden)],
                capture_output=True,
                timeout=10,
                env=execution_environment(),
            )
            events = store.events(s["id"])
            return dict(
                id=case["id"],
                mode=mode,
                learning=memory,
                status=s["status"],
                final=s.get("final"),
                passed=result.returncode == 0,
                workflow_completed=s["status"] == "completed",
                tokens=s["usage_tokens"],
                elapsed_seconds=round(time.monotonic() - started, 2),
                steps=s["steps"],
                tool_errors=sum(
                    "error" in e["data"].get("result", {})
                    for e in events
                    if e["kind"] == "tool_result"
                ),
                errors=[
                    e["data"]
                    for e in events
                    if e["kind"] == "tool_result"
                    and "error" in e["data"].get("result", {})
                ],
                plan=s.get("plan"),
                reflection=s.get("reflection"),
                reflection_error=s.get("reflection_error"),
                approvals=decisions,
                verification=s["verification"],
                model=provider.model,
                recalled_count=len(s.get("recalled_lessons", [])),
                stored_lessons=len(store.recall_lessons(root, case["task"])),
            )
        finally:
            store.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="evaluation/live-modes-results.json")
    p.add_argument("--limit", type=int, default=4)
    p.add_argument("--transfer", action="store_true")
    a = p.parse_args()
    report = {
        "scope": "Real model, fixed synthetic tasks; held-out assertions outside editable workspace. One attempt per case/mode. Not production task success rate. Local execution, not sandbox.",
        "dataset_sha256": digest(CASES),
        "results": [],
    }
    if a.transfer:
        with tempfile.TemporaryDirectory() as shared:
            for label, enabled in [
                ("seed", True),
                ("without_memory", False),
                ("with_memory", True),
            ]:
                result = run_case(CASES[0], "react", memory=enabled, shared=shared)
                result["condition"] = label
                report["results"].append(result)
                Path(a.out).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(
                    label,
                    result["passed"],
                    result["recalled_count"],
                    result["stored_lessons"],
                    flush=True,
                )
        return
    Path("evaluation/live-mode-tasks.json").write_text(
        json.dumps(CASES, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for case in CASES[: a.limit]:
        for mode in ("react", "plan_execute"):
            try:
                result = run_case(case, mode, memory=True)
            except Exception as exc:
                result = dict(
                    id=case["id"], mode=mode, passed=False, error=type(exc).__name__
                )
            report["results"].append(result)
            Path(a.out).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in {"plan", "reflection"}
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
