# 真懂 AI（TrueGrasp）

面向高校高等数学的自主教学 Agent。设计见《作品简介》《技术文档》，任务与分工见《开发计划》。

## 目录

| 目录 | 内容 |
|---|---|
| `backend/` | FastAPI 后端，uv 管理依赖，Python 3.13 |
| `frontend/` | Vite + React + TypeScript 前端，npm 管理依赖，Node 24 |

后端模块的划分与负责人见《开发计划》第 1 节。

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

## 提交前检查

CI 在每个 PR 和 `main` 的推送上运行下列检查，提交前先在本地跑一遍。

```bash
# 后端
cd backend
uv run ruff check .
uv run ruff format --check .   # 自动修复：uv run ruff format .
uv run pytest

# 前端
cd frontend
npm run lint
npm run format:check           # 自动修复：npm run format
npm run build
```
