# 开发指南

## 模块与接口约定

后端模块的划分与负责人见[开发计划](../开发计划.md)第 1 节。模块之间的契约（实体、Agent 输入输出、接口桩、事件类型）都在 `backend/app/contracts/`，改动前先和柴确认。HTTP 接口定义导出在 `backend/openapi.json`，启动后端后也可以在 `/docs` 查看。

## T03 编排内核

`backend/app/orchestration/` 实现 LangGraph 主图、辅导与验证子图、SQLite checkpoint、验证输入白名单和 SSE 广播总线。业务实体仍存入 `DATABASE_URL`，任务对话与等待点存入 `CHECKPOINT_DB_PATH`。部署按单进程运行，SSE 不跨进程广播、不回放历史；重新连接后通过 REST 读取当前状态。

编排私有的节点执行结果、待调度工作和题目展示记录存入业务库，在启动时自动创建对应表，T02 实体及 OpenAPI 不变。节点结果和调度工作与业务变更在同一事务中提交；启动后自动恢复尚未确认的工作。因此，无论中断发生在业务提交后、checkpoint 保存前，还是 checkpoint 保存后、后台调度前，都可继续执行。`finish/replan` 的业务终态已经提交时，只恢复有执行记录的未完成收尾；已结束任务仍不接受学生回复。

真实 Agent 分别在 T08、T09、T18 接入，T03 的桩 Agent 只在测试中使用。默认启动可访问健康检查和 SSE；未注入 Agent 时，执行任务接口返回 501，不产生模拟教学数据。

### 接入 Agent 与教学时钟

在应用组装处调用 `app.main.create_app(orchestration_dependencies=...)`，依赖字典支持以下字段：

| 字段 | 接口与责任 |
|---|---|
| `planner`、`tutor`、`verifier` | 实现 `contracts.agents` 中的三个 Protocol |
| `knowledge` | 实现 `contracts.knowledge.KnowledgeGraph`，辅导输入使用 `ancestors()` 构造允许回溯的知识点 |
| `teaching_date` | 无参函数，返回 T07 的当前教学日期；未注入时沿用任务 checkpoint 的日期，首次启动默认用任务创建日期 |
| `evidence_writer` | 同步函数 `(session, evidence) -> Evidence`，默认只追加并 flush；T19 可在同一事务中追加证据并迁移掌握状态，不能自行 commit |

生命周期内通过 `app.state.orchestrator` 获取运行时，`app.state.event_bus` 获取发布端。脚本和集成测试也可通过 `async with Orchestrator.open(engine, checkpoint_path, bus, **dependencies)` 管理运行时。业务库需先初始化，调用方负责释放传入的 engine 和 bus。

| 运行时入口 | 行为 |
|---|---|
| `await handle_event(event_id)` | 读取已提交的领域事件，调用规划、保存计划并后台启动当前到期的交互任务；重复处理同一事件不重新规划或重复建任务 |
| `await start_task(task_id, ...)` | 执行到等待输入或结束；已有等待点时不重复开场，失败节点可从 checkpoint 继续 |
| `await submit_message(task_id, MessageCreate(...))` | 用学生回复恢复等待点；作答的 `answering_item_id` 必须与当前待答题目一致，自由对话时为 `None` |
| `await get_messages(task_id)` | 从 checkpoint 读取本任务的 `ChatTurn` 列表及题目附件 |
| `await wait_idle()` | 等后台任务执行到等待点或结束；脚本和测试用此方法获取后台异常 |

`start_task()` 可显式传入 `teaching_date`、`dimensions` 和 `context_knowledge_point_id`。初次验证默认考查正确性、抗诱错、近迁移；仅偶发失误时只考查正确性，复测默认考查保持，跨章节预检由调用方明确指定维度和后续知识点。

规划创建的未来任务保持 `pending`；T07 提交 `TaskDue` 事件后，内核会启动该任务。班级讲解建议和转教师任务只保存和通知。辅导申请重规划时，当前任务取消，原因写入 `Task.reason`，并用 `TaskDue` 唤起一次规划；这次规划的新任务保持待办，不递归启动新一轮辅导。

验证 Agent 的出题输入只通过 `build_verifier_input()` 构造，包含目标知识点、维度、已确认错因的 ID 与描述、已用题目指纹和可选跨章节上下文。指纹覆盖该学生的历史作答及各会话中已展示的题目，包括尚未作答的题目，并在重启后保留；其他学生的展示记录不会混入。展示验证题时也检查指纹，防止不同题目 ID 掩盖重复题。验证评阅只接收独立测题及该题学生作答。诊断作答和假设状态仍由 T09 内部程序记录，T03 负责追加独立验证证据并生成 `AnswerSubmitted` 事件，不自行改变掌握状态。

### HTTP 与 SSE

已接通 `POST /api/tasks/{task_id}/start`、`GET /api/tasks/{task_id}/messages`、`POST /api/tasks/{task_id}/messages` 和 `GET /api/events`，请求与响应沿用 T02 OpenAPI。POST 校验后返回 `202 {"task_id": ...}`，后台执行；未知任务返回 404，任务已结束、未到期、回复题目不符或正在处理上一条消息时返回 409。

SSE 的 `data` 是既有 `StreamEvent` JSON，前端按 `type` 分发；每个连接都有独立队列，断开后释放。`token` 由辅导 Agent 流式发布，`message_done` 在 checkpoint 保存后发布；`task_changed` 和 `evidence_added` 在业务事务提交后发布。后台异常日志包含事件或任务 ID，可再次启动从失败节点继续；已提交的业务结果通过执行记录恢复，不重复创建任务、追加证据或生成重规划事件。

验收命令（使用临时 SQLite 和确定性桩 Agent，不需要模型密钥）：

```bash
cd backend
uv run pytest tests/test_orchestration.py -q
```

验收覆盖完整教学链路、验证信息隔离、两个等待阶段及业务提交/checkpoint/调度之间的中断恢复、跨会话未答题指纹去重、失败重试、事件与任务去重、重规划与取消、到期任务、HTTP 异步受理以及 SSE 广播和订阅释放。

## T04 知识图谱与内容管线

教学内容在 `content/lectures/`，每讲一个 YAML 文件，格式与填写说明见 [content/README.md](../content/README.md)，模板见 `content/template.yaml`。`app.knowledge` 提供以下命令（在 `backend` 目录运行）：

| 命令 | 作用 |
|---|---|
| `uv run python -m app.knowledge skeleton` | 从《高等数学目录》生成缺少的讲次文件，已有文件不覆盖 |
| `uv run python -m app.knowledge check` | 入库检查，逐条报告文件、条目 ID 和原因；有错误时退出码为 1 |
| `uv run python -m app.knowledge ingest` | 检查通过后写入业务库，只新增或更新，不删除 |
| `uv run python -m app.knowledge draft-probe <错因 ID>` | 用模型起草判别追问题，通过检查后导出到 `content/drafts/` 待教师审核 |

T05 合并前，入库检查跳过 `verify_item` 和 `check_distinct`，并给出提示；T06 合并前，`draft-probe` 无法调用模型。

后端启动时用 `NxKnowledgeGraph.from_engine()` 从业务库加载图谱，存入 `app.state.knowledge`，并作为编排的默认 `knowledge` 依赖。图谱加载后不再读库，重新入库后要重启后端。

## 提交前检查

CI 在每个 PR 和 `main` 的推送上运行下列检查，提交前先在本地跑一遍。

```bash
# 后端
cd backend
uv run ruff check .
uv run ruff format --check .   # 自动修复：uv run ruff format .
uv run pytest
uv run python -m app.openapi --check   # 改了接口后先运行 uv run python -m app.openapi
uv run python -m app.knowledge check    # 教学内容的入库检查

# 前端
cd frontend
npm run lint
npm run format:check           # 自动修复：npm run format
npm run build
```
