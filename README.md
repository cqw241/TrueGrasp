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
