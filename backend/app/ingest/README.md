# T17 作业导入

沿用 `POST /api/homework` 的 multipart `file` 和 `HomeworkImportResult`，只接收 CSV。
图片批次通过 Python 服务或 CLI 导入，不新增 HTTP schema。

CSV 为 UTF-8（支持 BOM），必填列 `student_id,item_id,student_answer`。
可选列 `student_name,stem,correct_answer,knowledge_point_id`。
学生答案和标准答案用 LaTeX，包含逗号或换行的字段按 CSV 规则引用。
已有学生、题目可以只提供必填列；新学生必须提供姓名，新题目必须提供题干与标准答案。
知识点可以省略，由模型从已入库知识点中选取；已有题目的知识点不可更改。
已存在的题目和学生不覆盖；同批次重复的学生/题目组合、冲突的题目内容整批拒绝。
重复提交整个文件会追加新的证据；现有接口没有幂等键，调用方需避免重复提交。

```csv
student_id,student_name,item_id,stem,correct_answer,student_answer
S01,学生一,hw.chain.1,求 sin(3x) 的导数,3\cos(3x),\cos(3x)
```

从 backend 目录运行（使用已有 DATABASE_URL 与 LLM 配置，教学日期必须明确）：

```powershell
python -m app.ingest csv homework.csv --teaching-date 2026-09-07
python -m app.ingest images homework-images.json --teaching-date 2026-09-07 --review-output review.json
```

图片 JSON 清单是数组，一条记录对应一个学生的一道题，多页作答可放多个图片：

```json
[
  {
    "student_id": "S01",
    "student_name": "学生一",
    "item_id": "hw.chain.1",
    "stem": "求 sin(3x) 的导数",
    "correct_answer": "3\\cos(3x)",
    "images": ["photos/S01-1.png", "photos/S01-2.png"]
  }
]
```

本地图片相对清单目录解析；也支持 HTTP(S) 和 image data URL。
调用 `app.llm.vision.transcribe_answer` 保留原作答，不替学生更正。
按技术文档，无疑问且可解析的公式直接导入；疑问或解析失败时整批不写入，退出码 2，
review.json 保留所有转写、疑问和需要确认标志。核对后在原清单相应记录中填写
`confirmed_answer`，重新导入；确认过的答案（包括文字解释）不重复 OCR。
其他输入/模型错误退出码 1，成功输出既有 HomeworkImportResult JSON，退出码 0。

Python 入口是 `HomeworkImporter(engine, llm).import_csv(data, teaching_date)`、
`import_rows(rows, teaching_date)` 和 `import_images(batch, teaching_date)`。
单独预览可调用 `transcribe_images(llm, batch)`。
服务默认使用 T19 `app.ledger.service.append_evidence`，也支持注入同签名的事务写入器。
必须先初始化业务库、导入知识点和错因库；不支持凭空创建知识点/错因。
新题由模型在内部 HomeworkAnalysis 中提取 ItemSpec、比较模式和变量，CSV/图片格式不变。
提取的 ItemSpec 先通过标准答案重算核验，通过后写入新题并用于工具判定；已有题目不覆盖。
不定积分按积分变量比较导数，忽略积分常数 C。没有核验过的题型时仍执行表达式等价检查，
只采纳工具“等价”的正向结果，不把 expr 不等价盲目当成错误；工具无法确定时保留模型初评。
模型错因仅是初步线索，不创建已确认假设；正确或无法判定的作答不记录命中错因。

整批学生、题目、Homework 证据、T19 掌握状态、一个 HomeworkImported 事件和已有编排
PendingWork 在同一事务提交。HTTP 提交后调用 recover_pending 唤起规划。
运行时默认每 0.25 秒监听其他连接（如 CLI）新建的持久化待办，因此已启动且空闲的服务
也会自动处理 CLI 导入，无需 HTTP 请求或重启。该监听适用于单进程，不占用业务 _jobs，
不会阻止 wait_idle；close 会取消并等待监听退出。通过 Orchestrator.open 的
pending_poll_interval 调整轮询间隔，设为 None 可关闭监听。
后台执行失败的工作保留待办并通过 wait_idle 报告错误；消费错误后也不自动无限重试，
可显式调用 handle_event/start_task 或重启运行时恢复。没有接入规划 Agent 时待办保留。
模型故障、非法知识点/错因、外键及写入错误均不留下半批业务数据。
