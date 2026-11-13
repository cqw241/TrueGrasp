# 真懂 AI（TrueGrasp）

面向高校高等数学的自主教学 Agent：主动发现学习问题、开展针对性辅导，通过独立验证和证据持续跟进学生的掌握情况。

## 文档

- [作品简介](作品简介.md)：项目背景、核心功能与应用场景。
- [技术文档](技术文档.md)：整体架构、技术选型与教学机制。
- [开发计划](开发计划.md)：任务安排、分工与完成标准。
- [开发指南](docs/development.md)：模块接入、接口约定与开发检查。

## 目录

| 目录 | 内容 |
|---|---|
| `backend/` | FastAPI 后端，uv 管理依赖，Python 3.13 |
| `frontend/` | Vite + React + TypeScript 前端，npm 管理依赖，Node 24 |
| `content/` | 数学教师编写的教学内容（YAML），填写说明见 `content/README.md` |

## 本地启动

后端（http://127.0.0.1:8000，接口文档在 `/docs`）：

```bash
cd backend
cp .env.example .env   # 填写 LLM_API_KEY
uv sync
uv run python -m app.auth_account teacher --role teacher
uv run uvicorn app.main:app --reload
```

前端（http://localhost:5173，`/api` 请求由 Vite 代理到后端）：

```bash
cd frontend
npm install
npm run dev
```

教师账号命令会交互式读取密码。打开前端后，教师从“班级管理”创建班级、添加或批量导入学生，再为每名学生生成一次性登录设置链接；学生通过链接设置密码后自动进入本人辅导台。顶部状态显示“实时连接正常”即实时事件通道已连通（窄屏显示状态圆点）。

历史学生若在启用班级归属前已存在，需要管理员明确选择学生和目标班级后执行一次迁移；迁移只处理该学生仍未分配的任务、证据、事件、题目图片和掌握记录，不会把无法判定的数据授权给全部教师：

```bash
cd backend
uv run python -m app.class_migration --class-id cls_xxx --student-id A
```

教师驾驶舱采用三列总览，点击模块标题可展开处理，点击“返回总览”或按 Esc 收起；筛选条件与填写中的处理说明会保留。窄屏自动改为纵向布局。界面改版和当前交付边界见 [工作台检查记录](docs/reviews/workspace-redesign.md)。

学生辅导台支持在待答题下拍照或选择图片（JPEG、PNG、WebP，最大 8 MB、2400 万像素）。识别后可在公式框中校对，再提交作答。后端需配置同时支持图片输入和 JSON 输出的 OpenAI 兼容模型；可在 `backend/.env` 中用 `VISION_LLM_BASE_URL`、`VISION_LLM_API_KEY` 和 `VISION_LLM_MODEL` 单独设置，未设置时沿用 `LLM_*`。配置模型密钥后，应用默认组装规划、辅导和验证 Agent；完整演示链路仍需预跑验收。

教师驾驶舱的“作业上传与审核”支持 CSV、JPEG/PNG/WebP、常见 PDF 和直接输入文字。结构化 CSV 保留原字段；其他材料由模型提取为草稿，保留原始文件、页码、作答及识别疑点，失败页可单独重试或补图。文字/一般 CSV 使用 `LLM_*`，图片和 PDF 使用上述视觉模型配置。每份草稿最多 6 个文件、总计 30 MiB，单图 8 MiB、单 CSV 10 MiB、单 PDF 20 MiB/12 页，合计最多 24 页/份材料及 5000 条作答。

教师驾驶舱的“作业上传与审核”绑定当前班级，AI 提取后可修改题干、参考答案、知识点、学生归属与原始作答，标记已核对或跳过，再发布已核对记录。缺参考答案时由教师补录；不确定内容可以继续留草稿。原文件、首次提取、人工修改和发布快照保留，发布回执显示正式导入及 Agent 规划状态。重复确认不会重复导入；任务只由现有 Agent 编排生成。旧个人草稿仍仅上传教师可见，明确绑定自己的班级后才能发布；绑定后不可切换班级。完整学生辅导与验证联调仍属 OPT-05。

启动时会自动新增发布回执与证据关联表，并补齐旧草稿 JSON 默认字段；不修改原学生档案或既有证据。已有 `POST /api/homework` 和 CLI 正式导入入口继续保留。实际审核发布接口与边界见[接口契约实施说明](docs/接口契约约定.md#8-opt-04-实施说明审核与确认发布)。
