# T14 演示操作手册

运行脚本使用现有教师、学生和教学时钟 API，不建教学任务、不写证据或掌握结论。
预跑和现场演示使用独立数据库，单实例运行后端。朱负责的 T16 提供目标和转交接口；
柴负责的 T13 展示实际证据。使用真实模型，密钥通过后端环境变量或未跟踪的
`backend/.env` 配置。预跑命令指向团队控制的后端，浏览器和命令行应连接同一实例。

在 `backend` 下先建隔离库、导入内容并启动应用，PowerShell 示例：

```powershell
$env:DATABASE_URL = 'sqlite:///./data/demo-small/truegrasp.db'
$env:CHECKPOINT_DB_PATH = './data/demo-small/checkpoints.db'
$env:TEACHING_CLOCK_MODE = 'demo'
$env:TEACHING_CLOCK_INITIAL_DATE = '2026-09-07'
uv run python -m app.knowledge ingest
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端，在仓库根目录执行。非默认端口在子命令前加 `--url http://127.0.0.1:端口`；
慢操作可用 `--wait 秒数`。Windows 可设置 `$env:PYTHONUTF8 = '1'` 保存 UTF-8 日志。
先按 [登录与权限说明](../../docs/development.md#t26-登录与权限)创建教师账号，
并设置 `$env:TRUEGRASP_DEMO_USER = '教师账号'`；在线命令会交互读取密码并在结束时退出。
也可在子命令前使用 `--username 教师账号`；自动化可用 `--password-stdin` 从标准输入提供密码，
不要把密码写入命令参数、脚本或版本库。`save`、`restore` 和 `capture` 是离线操作，不需要登录。

```powershell
$py = (Resolve-Path backend/.venv/Scripts/python.exe).Path
& $py -m demo check
& $py -m demo.scripts status
& $py -m demo.scripts lecture 2
& $py -m demo.scripts goal
& $py -m demo.scripts import-ch2 --student A --student B --student C --student S05 --student S14
& $py -m demo.scripts batch diagnosis_ch2 --student S05
```

先用小规模库验证诊断、独立验证及一次复测，确认接口和任务都正确后，新建
`demo-full` 业务库和 checkpoint，从头导入内容并启动。把上面的
`import-ch2 --student ...` 换成 `import-ch2`，即可导入全班。不要在小规模库追加全班，
也不要对超时的导入直接重发；先核对学生名单和后端日志。

| 教学日 | 操作 |
| --- | --- |
| 09-07 | 进入第 2 讲、创建或复用目标、导入作业。A/B/C 在学生辅导台完成真实诊断及辅导；其余计划追问用 `batch diagnosis_ch2`。 |
| 09-08 | `advance 2026-09-08`、`batch verification`；A/B/C 人工完成实际独立验证。小规模库用 `--student S05 --student S14`。 |
| 09-11 | `advance 2026-09-11`、`batch retest_friday`；B 由真人按公式代换的错法作答，核对实际复测和回溯证据。 |
| 09-14 至 09-16 | 按实际到期日推进，B 在辅导台执行先修回溯和后续验证；核对实际转交任务及证据。 |
| 10-26 | `advance 2026-10-26`、`lecture 9`、`batch precheck_ch9`。按系统实建的匹配任务作答；B 和其他被触发的人工角色在辅导台完成。 |
| 10-27 至 10-28 | 分别推进到当天后用 `batch ch9_followup`，核对 S05/S06 的跟进和转交。 |
| 现场 | 从验收快照恢复，推进到 11-02、进入第 10 讲。D 的初始答案由真人在终端输入，如 `import-d-live '2\pi/3'`；打开 D 的任务，先展示未作答的预测，再由 D 在辅导台提交追问。 |

批量命令只选当日的脚本作答；A/B/C 的后续作答以及 D 第十讲的作答不会被批量提交。
批量作答会匹配实际任务、知识点、轮次和当前待答题，用 T20 的 `answer_for` 核对
题目指纹、用途、维度，使用**实际题目 ID 和当前轮次**提交。输出 JSON Lines：
`answered` 是已读回的作答，`already_answered` 是对话中已有的同题同答，
`blocked` 带任务或学生与复现信息，命令以非零状态退出。它们都不等于教学验收通过。
辅导中额外出现的练习题，以及模型生成的不同验证题，由操作者按实际题目作答；
跟进阶段如实际轮次或日期与计划不同，记录偏差并核对证据，勿推进时钟硬凑结果。
人工排练提示用 `python -m demo batch 阶段 --include-human` 查看。

## 保存、核对和恢复

跑完第九讲跟进，确认后台处理完毕并**停止所有写这两个库的后端进程**，再保存。
原子地发布包含两份 SQLite 文件和 SHA-256 清单的快照；恢复只写入全新目录。
从快照导出实际记录后，用 T20 校验器验收；失败则将该快照标为排障材料。

```powershell
$rev = git rev-parse HEAD
& $py -m demo.scripts save backend/data/demo-full/truegrasp.db backend/data/demo-full/checkpoints.db backend/data/demo-accepted --revision $rev
& $py -m demo.scripts capture backend/data/demo-accepted/truegrasp.db backend/data/demo-full-actual.json --run-id demo-full
& $py -m demo check-run backend/data/demo-full-actual.json
& $py -m demo.scripts restore backend/data/demo-accepted backend/data/demo-d-take-1
```

将后端的 `DATABASE_URL`、`CHECKPOINT_DB_PATH` 同时指向
`demo-d-take-1` 的两份库，再启动后端。核对第九讲日期和界面、确认 D 还未作答，
然后按上表现场操作。要重录，从同一原始快照恢复到 `demo-d-take-2`，重新验证
同一待答题、预测先显示、提交后实际命中及状态更新。不要把已录过的恢复库再当原始快照。
业务库、checkpoint、临时日志和运行导出均放在 Git 忽略的 `backend/data/`。
