# 单实例服务器部署

这些配置用于 Alibaba Cloud Linux 3 上的 `https://8.148.178.2`，独立部署 TrueGrasp。Nginx 带宝塔 Lua 模块；标准 Nginx 部署需要调整 `lua_package_path` 和 MIME 文件路径。

## 目录与服务

| 内容 | 路径或名称 |
| --- | --- |
| 固定版本代码与前端构建 | `/opt/truegrasp/releases/<release>/` |
| 当前版本 | `/opt/truegrasp/current` |
| Python 3.13 | `/opt/truegrasp/runtime/python/bin/python3.13` |
| uv、lego 和续期脚本 | `/opt/truegrasp/tools/` |
| 环境配置，root:600 | `/etc/truegrasp/runtime.env` |
| 初始账号，root:600 | `/etc/truegrasp/accounts.json` |
| 业务、checkpoint、认证三个数据库 | `/var/lib/truegrasp/`，`truegrasp:700` |
| 后端 | `truegrasp.service`，非 root，单 worker，`127.0.0.1:18001` |
| HTTPS/HTTP 网关 | `truegrasp-gateway.service`，443 与 80；80 跳转 HTTPS |
| IP 证书续期 | `truegrasp-ip-tls.timer` / `truegrasp-ip-tls.service` |
| Nginx 日志 | `/var/log/truegrasp-gateway/` |

## 安装与配置

1. 本地完成前端构建，打包固定 Git revision 与 `frontend/dist/`；不上传 `.env`、本地数据库、缓存或 `node_modules`。服务器不需要 Node.js。
2. 创建 `truegrasp` 系统用户和上表目录。使用独立 Python 3.13，进入发布目录的 `backend/` 执行 `uv sync --frozen --no-dev --python /opt/truegrasp/runtime/python/bin/python3.13`。发布前在开发环境检查锁文件一致性，生产安装直接使用冻结的锁文件，不通过修改锁文件解决下载问题。
3. 在受限环境文件中配置 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 和 `LLM_RESPONSE_FORMAT=json_object`。旧 T04 配置中的 `DEEPSEEK_API_KEY` 与 `DEEPSEEK_MODEL` 对应当前的 `LLM_API_KEY` 与 `LLM_MODEL`；不要将 `${...}` 引用原样当作密钥或模型名。需要独立视觉模型时填写三个 `VISION_LLM_*` 字段，未填写时沿用主模型。
4. 生产配置至少包含：

   ```dotenv
   DATABASE_URL=sqlite:////var/lib/truegrasp/business.db
   CHECKPOINT_DB_PATH=/var/lib/truegrasp/checkpoints.db
   AUTH_DB_PATH=/var/lib/truegrasp/auth.db
   AUTH_COOKIE_SECURE=true
   AUTH_PUBLIC_ORIGIN=https://8.148.178.2
   TEACHING_CLOCK_MODE=demo
   TEACHING_CLOCK_INITIAL_DATE=2026-09-07
   ```

5. 显式加载该环境文件后执行 `python -m app.knowledge ingest`。设置 `PYTHONPATH` 指向 `backend/`，用该虚拟环境运行 `deploy/bootstrap.py`。脚本仅创建一个演示学生身份及两类账号，不填造掌握、证据、目标或任务；重复运行保留既有账号。生成账号文件留在服务器 root 受限目录，通过安全通道交付。
6. 将状态目录及文件交给 `truegrasp` 用户，环境与账号文件仍归 root。安装本目录中的 systemd、Nginx 配置和续期脚本。
7. TLS 使用有效的公网 IP SAN 证书。迁移现有 ACME 账号和证书时仅在服务器内部复制到 `/etc/truegrasp/acme/` 与 `/etc/truegrasp/tls/`，保持目录 700、私钥 600；不下载或提交私钥。安装配套 lego 二进制到 `/opt/truegrasp/tools/lego`。
8. 设置 current 软链接，执行 `nginx -t -p /var/lib/truegrasp-gateway/ -c /etc/truegrasp/nginx.conf` 与 `systemd-analyze verify`。然后启用 `truegrasp`、`truegrasp-gateway` 和 `truegrasp-ip-tls.timer`。

## 验收与维护

网络受限时可以在有网络的工作机准备离线依赖。用 `uv export --frozen --no-dev --no-emit-project --format requirements-txt` 导出带哈希的依赖，指定 Python 3.13 和 `x86_64-manylinux_2_28` 平台，通过 `uv pip sync --require-hashes --target <staging>` 准备 Linux 包。打包该目录，记录归档 SHA-256，上传后校验并安装到服务器独立 venv 的 `lib/python3.13/site-packages/`。不可上传 Windows `.pyd` 或使用 Windows venv 代替 Linux 运行时；跨平台 staging 中的启动脚本不用于生产，服务始终通过 `python -m uvicorn` 启动。安装后用服务器上的 Python 执行依赖检查和实际导入验证。

```sh
systemctl is-active truegrasp truegrasp-gateway truegrasp-ip-tls.timer
curl --fail http://127.0.0.1:18001/api/health
curl --fail https://8.148.178.2/api/health
openssl x509 -in /etc/truegrasp/tls/fullchain.pem -noout -dates -ext subjectAltName
systemctl list-timers truegrasp-ip-tls.timer
journalctl -u truegrasp -n 60 --no-pager
```

公网校验必须正常验证证书，不使用 `curl -k` 或关闭 TLS 校验。验证登录、Secure/HttpOnly/SameSite Cookie、学生权限、深链接刷新、静态资源、SSE 和真实模型业务；仅健康接口 200 不能证明完整教学链路通过。

续期每六小时检查一次，剩余不足 72 小时时通过 TLS-ALPN-01 短暂停止当前网关，安装新证书后恢复。trap 和 `ExecStopPost` 均负责恢复，只操作 `truegrasp-gateway`。当前证书尚有余量时，运行续期服务只检查和跳过签发；这不等于重新签发验收。

升级前停止应用，再一起备份业务库及其 WAL、checkpoint 和认证库。切换 current 到新发布，启动并验证；失败时切回旧发布。账号库与业务库分离，教学快照恢复不能覆盖账号库。

## 恢复此前项目

本次用户明确授权停止原业务。原发布目录未删除；停服前的 unit 状态与配置备份位于 `/opt/truegrasp/backups/pre-switch-20261112/`。原 `agent-guard-app`、`agent-guard-gateway`、`blackwall-api`、`mingjian-api`、`mingjian-web` 和旧证书 timer 停止并取消自启；SSH、MySQL 与管理面板保留。

如用户决定恢复此前部署，先停止并取消 TrueGrasp 网关、应用和续期 timer；确认 80、443、8080 已释放，再根据备份状态恢复旧 unit。证书若已经轮换，先将当前有效证书和 ACME 状态在服务器内部同步回旧项目路径，再恢复旧续期 timer。不要同时启用两个占用 443 的网关或两个 IP 续期 timer。
