# Repo Workbench Agent

一个面向本地代码仓库的 Python Coding Agent：根据任务读取代码、按需加载技能、提出文件差异，由人批准后修改并验证。覆盖代码理解、功能开发、缺陷修复和测试审查，不局限于修 Bug。

**当前状态：运行时、工具、网页审核与离线回归已完成。真实模型接口已实现，但尚未配置供应商 API 验收。** 离线模式的下一步调用来自预设脚本；live 模式才由模型根据工具结果选择后续动作。此仓库不宣称生产部署或模型任务成功率。

## 解决的问题

- 单次代码问答无法落实修改：用 `model → tool_calls → tool_result → model` 循环保留反馈，直到答复、审批暂停或预算耗尽。
- 工具结果不断增长：保存完整会话，发送前按完整工具交互组裁剪历史；长结果外置，按 ID 分段回读，工作笔记保留目标与进度。
- 自动编辑难以审查：将 `propose_edit` 与 `apply_edit` 分开，展示 diff、逐项授权，应用前核对 SHA-256，避免覆盖审批后发生的修改。
- 中途退出丢失工作：SQLite 保存会话、调用结果和审批；恢复时先处理未完成工具调用，已记录的结果直接回放。
- 模型声称完成不等于测试通过：单独记录代码 revision 和实际测试结果，修改后验证失效；未测试的修改明确标为 `completed_unverified`。

## 快速运行

Python 3.11+，建议在独立虚拟环境中安装：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
# 创建全新、可丢弃的演示目录；命令拒绝覆盖非空目录
repo-agent --workspace .runtime/demo init-demo
repo-agent --workspace .runtime/demo --state .runtime/demo-state --backend trusted-local --demo serve
```

打开终端输出的本地地址（包含一次生成的访问凭据）。创建任务“修复加法错误并测试”，点击运行，查看 diff 并批准，再继续；测试执行需要单独批准。此例只运行项目自建的示例代码。`trusted-local` 会在宿主机执行仓库测试，**不是沙箱**。默认后端为 `disabled`。

再次演示请新建空目录，否则已修复的示例不会重复产生同一修改。

### 配置真实模型

去掉 `--demo`，配置支持 Chat Completions 与 function calling 的接口：

```powershell
$env:REPO_AGENT_BASE_URL = 'https://你的服务地址/v1'
$env:REPO_AGENT_MODEL = '供应商支持工具调用的模型名称'
# 在本机终端安全设置 REPO_AGENT_API_KEY，不提交到 Git
repo-agent --workspace 'D:/你的项目副本' --state .runtime/live-state serve
```

程序只读取 `REPO_AGENT_*`，不会自动使用其他项目的 OPENAI / DASHSCOPE 密钥，也不会自动加载 `.env`。DeepSeek 等供应商可通过这三个变量接入；模型名称与接口兼容性以供应商文档为准。首次使用先选仓库副本、保持测试执行 disabled，核对修改建议，再选择执行后端。详细验收步骤见 [真实模型验收](docs/live-acceptance.md)。

## 架构与目录

```text
用户任务 / 追加问题
    ↓
SQLite 会话 → 上下文投影（任务 + 技能目录 + 笔记 + 历史交互组）
    ↓
ChatProvider → 工具参数校验 → 权限校验 → 执行 / 等待人工批准
    ↑                                         ↓
    └────────── 工具结果 + 审计事件 ────────────┘
                       ↓
               最终答复 + 独立测试状态
```

| 文件 | 职责 |
| --- | --- |
| `src/repo_agent/engine.py` | 动态工具循环、恢复、预算、重复失败终止、多轮追加 |
| `context.py` | 有界历史投影、工具调用/结果成组保留 |
| `tools.py` / `policy.py` | 10 个工具、Pydantic 参数契约、范围检查、审批及测试执行 |
| `store.py` | 会话、事件、调用结果、审批、产物归属的 SQLite 持久化 |
| `provider.py` | 可配置模型接口、超时与有限重试 |
| `skills/*/SKILL.md` | 四类技能的目录描述与按需加载正文 |
| `api.py` / `static/index.html` | 本地鉴权、任务面板、轨迹、差异审核、继续/停止 |
| `evaluation/` / `scripts/evaluate.py` | 固定合成任务与离线集成评测 |

没有引入 LangGraph、MCP 或多 Agent。编排由显式循环与持久化状态完成；模型决定调用哪些工具，权限与验收由代码决定。Skills 是指导流程的文本，不拥有绕过权限的能力。

## 工具与技能

10 个工具：`list_files`、`read_file`、`search_code`、`load_skill`、`propose_edit`、`apply_edit`、`run_tests`、`show_diff`、`save_notes`、`read_artifact`。

4 类 Skills：仓库理解、功能开发、缺陷诊断、测试审查。系统提示只放技能名称与说明，正文通过工具加载。工具 schema 目前全部随模型请求发送，未实现工具 schema 动态搜索。

## 验证

```powershell
pip install -r requirements-test.txt
python -m ruff check .
python -m unittest discover -s tests -v
python scripts/evaluate.py
```

本地 Windows：43 项测试，42 通过、1 项因无法创建符号链接跳过；8 个离线集成场景通过，包含真实临时文件修改及测试子进程。网页通过创建、审批、继续、刷新恢复和移动端无横向溢出检查。

这些结果验证机制，不衡量模型的独立规划或代码生成能力。`evaluation/offline-results.json` 记录数据 SHA-256、模式和逐项结果；真实模型评测尚未运行。详情见 [验收记录](docs/acceptance.md)。

## 边界

仅面向本机单操作人的仓库工作台，默认绑定 `127.0.0.1`，API 使用本地 Bearer 凭据。工作区锁为单进程内锁，不支持多 worker 互斥或多租户服务。

路径检查拒绝越界、受保护目录、符号/硬链接和常见密钥文件；这不是完整的数据防泄漏或操作系统隔离。测试默认禁用，Docker 后端不可用时拒绝执行，绝不自动降级到宿主机。Docker 运行策略已实现，当前机器没有 Docker，尚未进行实机验收。

历史使用**字符预算**，不是精确 token 压缩；没有宣称 KV Cache 命中收益、Mem0 长期记忆、自进化 Skills 或自动安全证明。更多约束见 [设计与权限](docs/design.md)。

## 参考与归属

架构学习参考 [MiniCode](https://github.com/LiuMengxuan04/MiniCode) 的工具循环、Skills 与审核理念。本仓库为独立 Python 实现，未复制 MiniCode 源码，也不是其官方版本。实现和测试由开发者与 AI 协作完成；README 和简历只描述已有代码及可复现证据，不将上游全部能力计入本项目。

MIT License。
