# MySQL 存储接入与事务设计

本次增加 SQLAlchemy Core + PyMySQL 后端，保留 SQLite；Alembic 记录表结构版本。实际本机验收连接独立 MySQL 8.0.12 实例 `127.0.0.1:3308/repo_workbench`，与原有 MySQL 服务分开。仓库提供 MySQL 8.4 Compose 和 CI 配置，但本轮没有实际运行 8.4，不应把配置存在当作验收通过。

## 数据到底放在哪里

| 表 | 保存什么 | 身份及查询依据 |
| --- | --- | --- |
| sessions | 原始 task、task_state、需求提取视图、笔记、预算、代码 revision、验证状态等 JSON；独立 status/version/时间列 | id；version 用于条件更新，区别于代码 revision |
| messages | user / assistant / tool 的完整消息 JSON，包括工具请求和结果 | (session, seq) 复合主键；seq 从 0 开始，保持 message:N 来源引用 |
| calls | 工具调用状态 running/waiting/done 与结果 JSON | (session, id) 复合主键 |
| approvals | edit 提案或 tests 执行规格 spec、审批决定、创建时间 | id；(session, kind, created) 索引 |
| events | 模型增量、工具开始/结束、审批、状态事件 | 自增 id；(session, id) 索引支持 SSE 游标 |
| artifacts | 超长工具结果的完整文本及归属 | id；MySQL LONGTEXT，代替本地 artifacts/*.txt |
| legacy_imports | 已导入源标识、条数与备份回执 | 源绝对路径的 SHA-256；避免重复导入 |
| alembic_version | 当前表结构版本 | 0001_storage |

代码文件、四个 SKILL.md、本机模型密钥和数据库密码仍在文件系统；没有把整个代码仓库上传到数据库。审批 diff / before / after 和部分工具输出本身会包含代码片段，因此数据库备份也应视为项目数据。原始历史不会因上下文裁剪而删除；模型看到的只是实时组装的上下文投影。

旧设计将 messages 和会话整体存入一列 JSON。现在消息单独按序存表，正常追加只写新增消息；程序为了兼容现有 Engine，读取时仍还原成包含 messages 的字典。当前仍会读取该会话全量历史，没有宣称实现消息分页或降低读取内存。

## 事务与并发

1. `Store.save` 使用 `UPDATE sessions ... WHERE id=:id AND version=:expected`。受影响行数必须为 1，否则抛出 StoreConflict，拒绝旧状态覆盖新状态。数据库 version 返回为内部 `_store_version`，与业务代码修改 revision 独立。
2. `complete_call` 将工具完成记录、tool 消息、笔记及 revision/验证状态、tool_result 事件放在同一短事务。注入事件写入异常时，前面的写入也全部回滚。外部工具执行在事务之外。
3. `wait_for_approval` 原子保存 waiting 调用、waiting_approval 会话和审批事件；`resolve` 将审批决定与审计事件一起提交。
4. 测试审批创建/领取先短暂锁定所属会话。MySQL 使用 SELECT FOR UPDATE；SQLite 使用 BEGIN IMMEDIATE。领取使用条件更新 allow → consumed，affected_rows 必须为 1。两个独立 Store 并发领取只有一个成功。
5. MySQL 使用连接池（5 个基础连接，最多 5 个溢出连接）、取连接超时、失效连接检测和连接回收。网络读写有超时，SQL 参数不写入异常日志。SSE 中同步数据库查询卸载到线程，避免阻塞事件循环。

模型网络调用、人工审批等待、测试子进程不持有数据库事务。没有对外部进程作 exactly-once 承诺：授权领取成功后、进程启动前崩溃，会保留 consumed 并要求显式复查；文件系统修改与数据库之间也不是同一个事务。工作区锁和后台调度仍在单进程内，不能因此宣称支持多 worker / 分布式执行。

## 启动和查看

默认不设置 `REPO_AGENT_DATABASE_URL` 时使用 `STATE/sessions-v2.db`。原 `sessions.db` 存在时会备份并一次性导入新 SQLite 文件，原文件不改写。

MySQL 模式配置：

```text
REPO_AGENT_DATABASE_URL=mysql+pymysql://repo_agent:URL编码后的密码@127.0.0.1:3308/repo_workbench?charset=utf8mb4
```

本机已写入 `.env.mysql.local`，该文件被 Git 忽略。不要把密码写入 README 或简历。程序不会自动扫描环境文件；PowerShell 可执行：

```powershell
. ./scripts/load-local-config.ps1
python scripts/database.py
./scripts/start-local.ps1 -Workspace '你的代码副本目录' -Python '你的python.exe' -Port 8023
```

`start-local.ps1` 加载 `.env`、`.env.requirements.local`、`.env.mysql.local`，按需切换数据库。默认不执行测试，可信练习仓库可显式指定 `-Backend trusted-local`。MySQL 不可达时明确失败，不静默退回 SQLite。测试执行后端 Docker 与数据库 MySQL 是独立配置。

本机独立 MySQL 数据目录为 `C:/Users/Administrator/Documents/RepoWorkbenchMySQL-20261021/data`（旧版 MySQL 初始化无法处理当前中文路径，故使用独立英文目录）。它是普通后台进程，没有修改原有 Windows MySQL 服务，也没有设成开机服务。电脑重启后先运行 `scripts/start-mysql-local.ps1`，然后启动 Agent。该脚本依赖本机忽略目录下的 instance.json；其他电脑请配置自己的 MySQL 或使用 Compose。

连接数据库查看可用 MySQL 客户端，地址 127.0.0.1、端口 3308、库名 repo_workbench、用户 repo_agent；密码只从本机配置读取。示例查询：

```sql
SELECT id, status, version FROM sessions ORDER BY created DESC;
SELECT session, seq, JSON_EXTRACT(data, '$.role') AS role FROM messages ORDER BY session, seq;
SELECT session, kind, decision FROM approvals ORDER BY created DESC;
SELECT id, session, kind FROM events ORDER BY id DESC LIMIT 20;
```

## 迁移旧数据

先停止旧 Agent 写入，再执行 `python scripts/database.py --import-sqlite 路径/sessions.db`。脚本通过 SQLite backup API 做一致性备份（包括 WAL），读取旧 artifacts 文件，在单一目标事务中导入；缺失产物、ID 冲突时拒绝，不覆盖目标数据。重复导入同一路径返回回执，不会继续同步后来在旧库产生的新数据；切换后不要同时写旧库。迁移 events 会重新分配 ID，网页应重新连接、清除旧 SSE 游标。

本机已导入 5 个旧状态目录，共 5 个会话、69 条消息、35 个工具调用、9 条审批、207 条事件；旧目录本次没有外置产物。各会话/调用/审批/事件内容逐项核对一致，备份位于 `.runtime/mysql-ui/migration-backups`。原 workspace 路径保留，网页仍按当前工作区过滤会话，不会把不同工作区的会话混在一起。

表结构由版本化迁移创建；初始迁移内冻结 schema，后续字段变更应新增迁移，不能修改已发布的旧版本。当前启动时检查并升级 schema，只应由单实例执行首次升级；实际多实例部署应独立运行迁移。MySQL DDL 可能隐式提交，不能把 DDL 迁移与业务事务的回滚保证混为一谈。

## 验收与限制

`tests/test_storage.py` 在 SQLite 与真实 MySQL 上运行同一套 13 项存储契约，覆盖独立消息、UTF-8/emoji、数据库产物回读、并发更新冲突、并发审批创建/领取、事务故障回滚、恢复与旧库迁移。`test_mysql_workbench.py` 复用 5 项网页接口契约，包括登录、SSE、审批自动继续和跨会话校验。整套本机测试 129 项，128 通过、1 项因 Windows 符号链接权限跳过；8 个离线任务通过。见 evaluation/mysql-acceptance.json 和 mysql-migration-results.json。

测试库独立为 `repo_workbench_test`，测试连接配置名 `REPO_AGENT_TEST_DATABASE_URL`；不提供时 MySQL 用例明确跳过。已增加 GitHub Actions MySQL 8.4 服务任务，未推送，尚无远端运行结果。没有做吞吐压测，不能宣称延迟或 QPS 提升；没有新增多用户身份、分布式调度、自动重试所有副作用或操作系统沙箱。
