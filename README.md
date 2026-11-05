# 真懂 AI（TrueGrasp）

面向高校高等数学的自主教学 Agent。设计见《作品简介》《技术文档》，任务与分工见《开发计划》。

## 目录

| 目录 | 内容 |
|---|---|
| `backend/` | FastAPI 后端，uv 管理依赖，Python 3.13 |
| `frontend/` | Vite + React + TypeScript 前端，npm 管理依赖，Node 24 |

后端模块的划分与负责人见《开发计划》第 1 节。模块之间的契约（实体、Agent 输入输出、接口桩、事件类型）都在 `backend/app/contracts/`，改动前先和柴确认。HTTP 接口定义导出在 `backend/openapi.json`，启动后端后也可以在 `/docs` 查看。

## 本地启动

后端（http://127.0.0.1:8000，接口文档在 `/docs`）：

```bash
cd backend
cp .env.example .env   # 填写 LLM_API_KEY
uv sync
uv run uvicorn app.main:app --reload
```

前端（http://localhost:5173，`/api` 请求由 Vite 代理到后端）：

```bash
cd frontend
npm install
npm run dev
```

页面显示“后端状态：ok”即前后端已连通。

## T03 编排内核

`backend/app/orchestration/` 实现 LangGraph 主图、辅导与验证子图、SQLite checkpoint、验证输入白名单和 SSE 广播总线。业务实体仍存入 `DATABASE_URL`，任务对话与等待点存入 `CHECKPOINT_DB_PATH`。部署按单进程运行，SSE 不跨进程广播、不回放历史；重新连接后通过 REST 读取当前状态。

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

验证 Agent 的出题输入只通过 `build_verifier_input()` 构造，包含目标知识点、维度、已确认错因的 ID 与描述、已用题目指纹和可选跨章节上下文。指纹覆盖该学生的历史作答和当前会话展示过的题目，不混入其他学生的作答；验证评阅只接收独立测题及该题学生作答。诊断作答和假设状态仍由 T09 内部程序记录，T03 负责追加独立验证证据并生成 `AnswerSubmitted` 事件，不自行改变掌握状态。

### HTTP 与 SSE

已接通 `POST /api/tasks/{task_id}/start`、`GET /api/tasks/{task_id}/messages`、`POST /api/tasks/{task_id}/messages` 和 `GET /api/events`，请求与响应沿用 T02 OpenAPI。POST 校验后返回 `202 {"task_id": ...}`，后台执行；未知任务返回 404，任务已结束、未到期、回复题目不符或正在处理上一条消息时返回 409。

SSE 的 `data` 是既有 `StreamEvent` JSON，前端按 `type` 分发；每个连接都有独立队列，断开后释放。`token` 由辅导 Agent 流式发布，`message_done` 在 checkpoint 保存后发布；`task_changed` 和 `evidence_added` 在业务事务提交后发布。后台异常日志包含事件或任务 ID，任务不会被标记为完成，可再次启动从失败节点继续。

验收命令（使用临时 SQLite 和确定性桩 Agent，不需要模型密钥）：

```bash
cd backend
uv run pytest tests/test_orchestration.py -q
```

验收覆盖完整教学链路、验证信息隔离、两个等待阶段的重启恢复、失败重试、事件与任务去重、重规划与取消、到期任务、HTTP 异步受理以及 SSE 广播和订阅释放。

## 提交前检查

CI 在每个 PR 和 `main` 的推送上运行下列检查，提交前先在本地跑一遍。

```bash
# 后端
cd backend
uv run ruff check .
uv run ruff format --check .   # 自动修复：uv run ruff format .
uv run pytest
uv run python -m app.openapi --check   # 改了接口后先运行 uv run python -m app.openapi

# 前端
cd frontend
npm run lint
npm run format:check           # 自动修复：npm run format
npm run build
```
