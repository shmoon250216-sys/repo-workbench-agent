"""Fixed synthetic retrieval benchmark using real local embedding inference."""

import json
import time
from pathlib import Path

from repo_agent.deliberation import terms
from repo_agent.memory_retrieval import HybridMemory, bm25, lesson_text

ITEMS = [
    (
        "async",
        "同步SDK阻塞事件循环",
        "将阻塞调用卸载到工作线程，保持异步服务响应",
        "仅适用于线程安全SDK，CPU密集计算应另行处理",
        ["访问第三方服务时其他请求也被卡住了", "同步SDK阻塞事件循环"],
    ),
    (
        "approval",
        "审批后的文件被外部修改",
        "应用补丁前重新核对SHA256，失效时读取文件并重新提出修改",
        "针对过期修改提案，不重复提交旧补丁",
        ["批准以后有人又改了代码，不能覆盖他的新内容", "审批后文件变化SHA256"],
    ),
    (
        "parser",
        "空项直接转整数导致异常",
        "去除首尾空白并过滤空项后再执行int转换",
        "仅适用于业务允许忽略空项的列表解析",
        ["连续两个逗号之间没有数字，转换就崩了，允许跳过空项", "parser空项整数转换"],
    ),
    (
        "retry",
        "远端请求触发429限流",
        "尊重Retry-After并采用有界退避重试",
        "仅重试暂时性故障，不重试401鉴权错误",
        ["接口说调用太频繁了，不能立即反复请求", "429 Retry-After退避"],
    ),
    (
        "secret",
        "日志意外输出访问凭据",
        "日志过滤Authorization和API密钥，使用占位信息定位故障",
        "凭据不得进入审计输出",
        ["排查接口时把令牌打印出来了，需要防止泄露", "Authorization日志密钥"],
    ),
    (
        "transaction",
        "进程中断导致工具结果与对话记录不一致",
        "工具完成记录和消息追加在同一数据库事务提交",
        "数据库内写入可原子提交，外部副作用需独立恢复策略",
        ["突然断电后结果保存了但是聊天里没有，需要一起提交", "数据库事务工具结果消息"],
    ),
    (
        "stale",
        "测试通过后代码又发生变化",
        "最终完成前核对测试时工作区指纹，变化后使旧测试结果失效",
        "不能用旧版本的测试结果证明新代码正确",
        ["刚跑绿了但文件随后被改了，还能说验证成功吗", "测试通过代码变化指纹"],
    ),
    (
        "path",
        "工具路径跳出允许的工作区",
        "解析规范路径并检查是否处于授权根目录内，拒绝越界及危险链接",
        "适用于文件读取与修改工具",
        ["有人给读取工具传了../../private，我要限制访问范围", "路径工作区越界"],
    ),
]


def main():
    rows = [
        {
            "id": i,
            "data": {
                "summary": failure,
                "failure": failure,
                "remedy": remedy,
                "applicability": scope,
            },
        }
        for i, failure, remedy, scope, _ in ITEMS
    ]
    cases = [
        {"query": q, "relevant": [i], "type": "positive"}
        for i, _, _, _, qs in ITEMS
        for q in qs
    ]
    cases += [
        {"query": q, "relevant": [], "type": "unrelated"}
        for q in [
            "请推荐周末的旅游景点",
            "今天晚饭怎么做红烧鱼",
            "帮我写一首关于月亮的诗",
            "安排下周的健身计划",
        ]
    ]
    cases += [
        {
            "query": "整数解析遇到空项必须报错，严禁忽略空项",
            "relevant": [],
            "type": "conflicting",
        },
        {
            "query": "同步SDK线程不安全，不允许移到线程池中运行",
            "relevant": [],
            "type": "conflicting",
        },
    ]
    dataset = {"lessons": rows, "cases": cases}
    Path("evaluation/memory-dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    retriever = HybridMemory()
    texts = [lesson_text(r["data"]) for r in rows]
    results = []

    def cache(row, value):
        row["data"]["_embedding"] = value

    for case in cases:
        # Legacy matching ignores new embedding metadata: compare original textual data only.
        old = sorted(
            [
                (
                    len(
                        terms(case["query"])
                        & terms(
                            json.dumps(
                                {
                                    k: v
                                    for k, v in r["data"].items()
                                    if not k.startswith("_")
                                },
                                ensure_ascii=False,
                            )
                        )
                    ),
                    r["id"],
                )
                for r in rows
            ],
            key=lambda x: -x[0],
        )
        sparse = bm25(case["query"], texts)
        start = time.perf_counter()
        chosen, report = retriever.rank(case["query"], rows, cache)
        elapsed = (time.perf_counter() - start) * 1000
        assert report["mode"] == "hybrid_rrf", report
        results.append(
            dict(
                **case,
                legacy=[i for score, i in old if score > 0][:3],
                bm25=[
                    rows[i]["id"]
                    for i in sorted(range(len(rows)), key=lambda i: -sparse[i])
                    if sparse[i] > 0
                ][:3],
                hybrid=[r["id"] for r in chosen],
                milliseconds=round(elapsed, 2),
                diagnostics=report,
            )
        )
    metrics = {}
    for method in ["legacy", "bm25", "hybrid"]:
        positive = [x for x in results if x["type"] == "positive"]
        negative = [x for x in results if x["type"] == "unrelated"]
        conflict = [x for x in results if x["type"] == "conflicting"]
        metrics[method] = {
            "recall_at_3": sum(
                bool(set(x[method]) & set(x["relevant"])) for x in positive
            )
            / len(positive),
            "precision_at_3": sum(
                len(set(x[method]) & set(x["relevant"])) / max(1, len(x[method]))
                for x in positive
            )
            / len(positive),
            "unrelated_rejected": sum(not x[method] for x in negative),
            "unrelated_total": len(negative),
            "conflicting_injected": sum(bool(x[method]) for x in conflict),
            "conflicting_total": len(conflict),
        }
    report = {
        "scope": "Synthetic 8-lesson/22-query fixture, real local BGE embedding. No LLM applicability judge, no task-success claim. Fixed cosine floor 0.6, RRF k=60. Not a held-out production benchmark.",
        "model": "BAAI/bge-small-zh-v1.5",
        "metrics": metrics,
        "results": results,
    }
    Path("evaluation/memory-results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
