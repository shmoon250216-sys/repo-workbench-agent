# 规划与复盘：验收结果（2026-10-31）

4 个自建合成仓库任务 × 2 种模式，每个条件仅执行一次；DeepSeek Flash 真实调用，固定温度0，同为30步、100,000累计Token阈值。两种模式都开启复盘，独立断言在可编辑目录外。不是企业任务成功率。

| 模式 | 独立断言通过 | 工作流完成 | 工具错误 | 平均总Token | 平均秒数 |
|---|---:|---:|---:|---:|---:|
| react | 4/4 | 4/4 | 0 | 31518 | 23.64 |
| plan_execute | 4/4 | 4/4 | 0 | 70349 | 39.75 |

两种模式均完成这组任务，但规划模式Token与耗时更高，因此保留用户选择，默认ReAct；不能宣称成功率提升。

经验复用：`live-memory-transfer.json` 中 seed、without_memory、with_memory 均通过；with_memory 召回1条历史经验，不开记忆的对照召回0条。证明跨会话复用链路工作，不证明学习收益。

机制回归：143项，142通过，1项Windows符号链接权限跳过；包含真实MySQL。另有8个离线集成场景及11项网页交互检查通过。

开发过程保留：
- `live-modes-pilot-25k.json`：规划模式受到预算和无法正确引用长调用ID影响；4项中仅1项代码通过独立断言，0项工作流完整完成。
- `live-modes-diagnostic*.json`：单任务诊断，未作为最终成绩。
- `live-modes-60k.json`：短证据编号消除了工具参数错误，但部分任务仍受预算约束，另一次ReAct出现模型接口错误。
- `live-modes-results.json`：100k试跑，发现一次提前结束导致计划未标记完成。随后增加最多2次结束前纠正。
- `live-modes-final.json`：最终实现的完整对照，全部样本保留；不能与试跑跨预算结果混写成算法提升。

主要报告：[最终对照](live-modes-final.json)、[经验复用](live-memory-transfer.json)、[机制回归](plan-reflection-regression.json)。
