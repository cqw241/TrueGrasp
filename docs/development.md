# 开发指南

## 模块与接口约定

后端模块的划分与负责人见[开发计划](../开发计划.md)第 1 节。模块之间的契约（实体、Agent 输入输出、接口桩、事件类型）都在 `backend/app/contracts/`，改动前先和柴确认。HTTP 接口定义导出在 `backend/openapi.json`，启动后端后也可以在 `/docs` 查看。

## T07 教学时钟与章前预检

`app.clock.service.TeachingClock` 提供 `state()`、`current_date()`、`await scan()`、
`await advance(days)` 和 `await enter_lecture(lecture)`。调用前执行 `init_db(engine)`，
传入已加载的 `KnowledgeGraph` 和事件发布端；可将 `wake` 绑定到
`runtime.recover_pending`。应用启动时自动组装为 `app.state.clock`，并默认将
`current_date` 注入编排层；显式传入的 `teaching_date` 依赖仍优先。

- `TEACHING_CLOCK_MODE=demo`（默认）：教学日期、当前讲次保存在业务库。首次初始化使用
  `TEACHING_CLOCK_INITIAL_DATE`（默认 `2026-09-07`），当前讲次从 1 开始；重启不重置。
- `TEACHING_CLOCK_MODE=real`：使用 `Asia/Shanghai` 当日日期，每 30 秒检查是否跨日，
  跨日扫描到期任务；手动推进返回 409。切换模式保留讲次，切回 demo 从最后保存的日期继续。
- `GET /api/clock` 返回现有 `ClockState`；`POST /api/clock/advance` 接收 `{"days": 4}`；
  `POST /api/clock/lecture` 接收 `{"lecture": 9}`。HTTP 请求与响应契约不变。

启动、推进日期和进入章节都会扫描 `pending`、到期日期不晚于当前教学日期的任务，
写入 `TaskDue` 领域事件。没有到期日期的任务不由时钟调度，`active/done/cancelled`
任务也不重复启动。每个任务只生成一次到期通知；事件、通知记录、编排 `PendingWork`
同事务提交。缺少 T08 规划 Agent 时保留待办，待接入后恢复，不生成模拟规划。

进入章节时，T07 按 T02 的 `prerequisites()` 契约检查**直接先修**，逐学生计算三条规则：
先修有已确认错因、先修未巩固、已确认错因的 `recurs_in` 指向新知识点。
确诊判断使用 `ledger.queries.confirmed_hypotheses()`，与 T08 同口径：按
`(task_id, error_evidence_id)` 分组，只有一个未被排除的候选且其状态为 `SUPPORTED`
时才确诊；其他候选为 `PENDING` 或 `SUPPORTED` 时均未确诊，`slip` 不作为确认错因。
缺失掌握记录按未巩固处理，`consolidated/integrated` 均满足巩固门槛。
T07 直接创建确定性预检任务；T08 消费 `LectureEntered` 时应复用这些任务，避免重复创建；其确诊查询可复用上述共享函数。

预检的 `knowledge_point_id` 是待复查的旧知识点，私有 `PrecheckContext` 保存新章节目标。
编排层在自动调度、手动启动和恢复时将其作为 `context_knowledge_point_id` 传入验证白名单，
默认维度为 `cross_chapter`。例如链式法则旧错指向第九讲多元复合求导时，仍检验原错因。
任务保留触发理由及依据证据；以“讲次、学生、旧知识点、新目标”去重。
重复进入章节允许补建新命中组合，已有任务结束或取消也不自动重建。
已掌握但有历史确认错因的学生仍会触发预检；缺失记录时名单可能覆盖全班，不能硬编码演示名单。

提交后通过现有 `ClockAdvanced`、`TaskChanged` 刷新前端。SSE 不回放；数据库事件与待办
承担恢复责任。私有表启动时自动创建，无需修改既有表或公共契约；仍按单进程部署。

验收：`cd backend && uv run pytest tests/test_clock.py -q`。覆盖日期推进、启动与跨日补扫、
事务回滚及去重、三条规则的精确名单、真实第 1→2→9 讲内容，以及独立验证上下文恢复。

## T03 编排内核

`backend/app/orchestration/` 实现 LangGraph 主图、辅导与验证子图、SQLite checkpoint、验证输入白名单和 SSE 广播总线。业务实体仍存入 `DATABASE_URL`，任务对话与等待点存入 `CHECKPOINT_DB_PATH`。部署按单进程运行，SSE 不跨进程广播、不回放历史；重新连接后通过 REST 读取当前状态。收到 Ctrl+C 或 SIGTERM 时立即结束 SSE 流，否则 uvicorn 会一直等连接关闭，旧进程不退出，`--reload` 也会卡住。

编排私有的节点执行结果、待调度工作和题目展示记录存入业务库，在启动时自动创建对应表，T02 实体及 OpenAPI 不变。节点结果和调度工作与业务变更在同一事务中提交；启动后自动恢复尚未确认的工作。因此，无论中断发生在业务提交后、checkpoint 保存前，还是 checkpoint 保存后、后台调度前，都可继续执行。`finish/replan` 的业务终态已经提交时，只恢复有执行记录的未完成收尾；已结束任务仍不接受学生回复。

真实 Agent 分别在 T08、T09、T18 接入，T03 的桩 Agent 只在测试中使用。配置模型密钥后，应用自动组装 T09 辅导 Agent 和 T18 验证 Agent，共享模型客户端；显式传入对应依赖（包括 `None`）可覆盖各自的装配行为。默认启动可访问健康检查和 SSE；缺少执行任务所需的 Agent 时返回 501，不产生模拟教学数据。

### 接入 Agent 与教学时钟

在应用组装处调用 `app.main.create_app(orchestration_dependencies=...)`，依赖字典支持以下字段：

| 字段 | 接口与责任 |
|---|---|
| `planner`、`tutor`、`verifier` | 实现 `contracts.agents` 中的三个 Protocol |
| `knowledge` | 实现 `contracts.knowledge.KnowledgeGraph`，辅导输入使用 `ancestors()` 构造允许回溯的知识点 |
| `teaching_date` | 无参函数，应用默认注入 T07 当前教学日期。直接使用 `Orchestrator.open` 且未注入时，沿用任务 checkpoint 的日期，首次启动用任务创建日期 |
| `evidence_writer` | 同步函数 `(session, evidence) -> Evidence`，默认使用 T19 账本，在同一事务中追加证据并迁移掌握状态，不能自行 commit；规则见 [账本说明](ledger.md) |

生命周期内通过 `app.state.orchestrator` 获取运行时，`app.state.event_bus` 获取发布端。脚本和集成测试也可通过 `async with Orchestrator.open(engine, checkpoint_path, bus, **dependencies)` 管理运行时。业务库需先初始化，调用方负责释放传入的 engine 和 bus。

| 运行时入口 | 行为 |
|---|---|
| `await handle_event(event_id)` | 读取已提交的领域事件，调用规划、保存计划并后台启动当前到期的交互任务；重复处理同一事件不重新规划或重复建任务 |
| `await start_task(task_id, ...)` | 执行到等待输入或结束；已有等待点时不重复开场，失败节点可从 checkpoint 继续 |
| `await submit_message(task_id, MessageCreate(...))` | 用学生回复恢复等待点；作答的 `answering_item_id` 必须与当前待答题目一致，自由对话时为 `None` |
| `await get_messages(task_id)` | 从 checkpoint 读取本任务的 `ChatTurn` 列表及题目附件 |
| `await wait_idle()` | 等后台任务执行到等待点或结束；脚本和测试用此方法获取后台异常 |

`start_task()` 可显式传入 `teaching_date`、`dimensions` 和 `context_knowledge_point_id`。初次验证默认考查正确性、抗诱错、近迁移；仅偶发失误时只考查正确性，复测默认考查保持，T07 创建的跨章节预检自动读取持久化上下文；其他调用方可明确指定维度和后续知识点。

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

## T10 前端基础

前端在 `frontend/src/`，路径别名 `@/` 指向该目录。三类页面的路由是 `/teacher`、`/student`、`/decision`，按路由拆包；T11 至 T13 直接替换 `pages/` 下对应的页面组件。

| 位置 | 内容 |
|---|---|
| `api/schema.d.ts` | 由 `backend/openapi.json` 生成，不要手改。后端接口变化后在 `frontend` 目录运行 `npm run gen:api`，CI 会检查两者一致 |
| `api/client.ts` | `api.useQuery('get', '/api/clock')`、`api.useMutation(...)`：路径、参数和返回值都有类型。非 2xx 响应统一抛出 `ApiError`（`status`、`message` 取自后端的 `detail`），网络错误的 `status` 为 0 |
| `api/stream.ts`、`hooks/use-event-stream.ts` | `useStreamEvent('task_changed', handler)` 订阅一种 SSE 事件；`useStreamStatus()` 读取连接状态。全站共用一条连接，出错后重新连上时自动让所有查询重新拉取 |
| `components/math/` | `<Tex latex display?>` 渲染一段 LaTeX；`<MathText text>` 渲染夹杂 `$…$`、`$$…$$`、`\(…\)`、`\[…\]` 的文字（题干、辅导消息）；`<MathInput value onChange onSubmit?>` 是受控的 MathLive 输入框，始终显示 `value`，父组件不接受或改写输入时随之还原；回车触发 `onSubmit` |
| `components/states.tsx` | `EmptyState`、`ErrorState`、`LoadingState`；`<QueryView query>` 统一处理一个查询的加载、出错和无数据。后端返回 501 时显示“功能开发中” |
| `components/layout/app-layout.tsx` | 布局与 `PageHeader`。桌面（≥1024px）固定侧边栏，平板和手机用顶部栏加抽屉导航 |
| `lib/labels.ts` | 后端枚举值到中文名称的映射，以及 `lectureLabel(9)` → “第九讲” |

shadcn/ui 组件用 `npx shadcn@latest add <组件>` 添加到 `components/ui/`。项目使用 TypeScript 7，它不提供旧版编译器 API，所以 `gen:api` 通过 `npx` 在独立环境中运行 openapi-typescript 和 TypeScript 5。

开发服务器的 `/api` 代理会立即转发 SSE 的响应头，否则在第一条事件到达之前，`EventSource` 一直处于连接中；后端异常断开时，代理也会断开浏览器一侧的连接，让 `EventSource` 重连（见 `vite.config.ts`）。生产环境的反向代理（T27）要关闭对 `/api/events` 的缓冲；后端已经发送了 `X-Accel-Buffering: no`。

## T11 学生辅导台

页面在 `frontend/src/pages/student-desk.tsx`：选择学生后分“任务”和“我的进度”两个标签页。任务列表只列学生能作答的类型（辅导、验证、复测、章前预检、主动提问），进行中的排在前面并默认打开。`Task.reason` 是给教师看的规划理由，不在学生端显示。

| 位置 | 内容 |
|---|---|
| `components/student/task-session.tsx` | 一个任务的对话：`GET /api/tasks/{id}/messages` 的记录，加上正在生成的回复。`token` 事件逐段拼到临时气泡，`message_done` 后重新拉取对话、清掉气泡。`ChatTurn.hint_level`（1–3）和 `solution` 显示为“提示 n/3 · 层级”“完整解答” |
| `components/math/task-answer.tsx` | 作答区：公式输入、拍照作答。识别结果 `doubts` 为空且学生还没写答案时直接提交进入诊断；有疑问时列出疑问，学生核对后再提交。T06 的 `requires_confirmation` 恒为真，所以这里只看 `doubts` |
| `components/student/ask-question.tsx` | 主动提问：输入或拍下题目，可附上自己的尝试，提交后打开新建的 `question` 任务，提示在该任务的对话里展示 |
| `components/student/progress.tsx` | 个人进度：各知识点掌握状态按讲分组，以及最近的学习时间线；收到本人的 `mastery_changed` 时刷新 |

主动提问的接口已在 T11 定好，后端实现留给 T15：

- `POST /api/students/{id}/questions`，请求体 `QuestionCreate {content, attempt}`，返回新建的 `Task`（`type = question`），目前返回 501。T15 实现时还要把 `question` 加进编排层可交互的任务类型（`runtime.py` 的 `INTERACTIVE_TYPES`），并在对话消息里写入 `hint_level` / `solution`，`get_messages` 已经透传这两个字段。
- `POST /api/students/{id}/question-photo` 识别题目照片，返回 `Transcription`，不创建提问。它和作答照片共用 `_transcribe_upload`（格式、大小、解码检查与视觉模型配置），只是不检查作答轮次。

## T12 教师驾驶舱

页面在 `frontend/src/pages/teacher-cockpit.tsx`，各面板在 `frontend/src/components/cockpit/`。每个面板单独请求数据，后端接口返回 501 时只有该面板显示“功能开发中”，其余面板照常工作。

| 面板 | 接口 | 后端实现 |
|---|---|---|
| 教学进度 | `GET /api/clock` | T07 |
| 班级达标率 | `GET /api/class/overview` | T19，已接通 |
| 教学目标 | `GET`、`POST /api/goals` | T16 |
| 全书知识热力图 | `GET /api/class/heatmap` | T19，已接通 |
| 共性问题 | `GET /api/class/issues` | T08 |
| 转交列表与证据链 | `GET /api/escalations`、`POST /api/escalations/{task_id}/resolve` | T16 |
| 干预计划 | `GET /api/tasks`（已接通）、`GET /api/tasks/controls` | T24 |
| 修改与覆盖计划 | `PATCH /api/tasks/{task_id}`（`TaskOverride`） | T16 |
| 接管辅导、交还 Agent | `POST /api/tasks/{task_id}/takeover`、`POST /api/tasks/{task_id}/handback`（返回 `TaskControl`） | T24 |
| 待审核错因与采纳 | `GET /api/misconceptions/pending`、`POST /api/misconceptions/{misconception_id}/adopt`（`AdoptMisconception`） | T16 |

`TaskOverride`、`TaskControl`、`TakeoverRequest`、`AdoptMisconception` 和后四行的路由由 T12 以接口桩的形式加入 `contracts/api.py` 与 `api/teacher.py`，它们是前端对接用的初稿，T16、T24 实现时可以调整，调整后重新生成 `openapi.json` 和 `schema.d.ts`。`GET /api/tasks/controls` 只返回教师接管或覆盖过的任务；它返回 501 时，计划列表照常显示，只是没有“教师接管”“已覆盖”标记。

写操作成功后，前端让相关查询重新拉取；收到 SSE 的 `task_changed`、`evidence_added` 时，也会刷新任务、转交、达标率和热力图。热力图用 ECharts 按需引入，只打包进驾驶舱页面。

## T13 决策证据视图

`/decision` 可选择学生、辅导任务和验证知识点，展示诊断假设与逐题预测、学习时间线、六维验证结果和跨章节路径。证据按需读取；可并排打开所选辅导任务的学生会话。诊断及时间线由相关 SSE 事件刷新，断线重连沿用全局查询同步。时间线中的回溯条目来自已开始或完成的先修回溯验证任务及其规划上下文；不会将普通验证或尚未开始的任务标为回溯。有作答时使用首条作答日期，无作答时标明任务创建日期。六维结果区分无证据和已有证据但无法判定。

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
npm run gen:api                # 改了接口后重新生成 API 类型
npm run lint
npm run format:check           # 自动修复：npm run format
npm run build
```

## T09 诊断辅导 Agent

实现、注入、持久化与验收说明见 [辅导模块说明](../backend/app/agents/tutor/README.md)。A/B/C 使用真实 C2 追问题，假设由程序逐项更新；诊断接口已接通并返回未答题预测。连续无进展时通过现有重规划出口附上允许的先修路径，不直接创建后续任务。T08 接入前，完整辅导链路仍需注入规划实现。
