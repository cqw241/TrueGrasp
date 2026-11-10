# T20 合成学生与演示输入计划

实际运行见 [T14 操作手册](scripts/README.md)。

这里只提供可审校的**输入**，没有已完成的预跑、证据账本、掌握状态或数据库快照。
`check` 成功只表示计划的数学与计数一致。真实结论由 T14 在真实系统提交作答、T19
按证据迁移状态、T21 验收后产生。40 名学生全部为合成身份，不代表真实教学效果。

## 文件与调用

- `data/scenario.json`：版本 1 的名单、三类第二讲行为、人工角色和预期跟进名单。
- `dataset.py`：只读 loader，引用实际 C2 YAML；展开 270 条作答输入和 15 道涉及的题。
- `__main__.py`：独立 CLI；不调用模型、网络、业务库，不创建任务或写账本。
- `observed.py`：检查 T14 从真实系统导出的记录，拒绝把输入计划当成运行结果。

在仓库根目录运行，使用装有 backend 依赖的 Python（工作区可用
`E:/TrueGrasp/backend/.venv/Scripts/python.exe`）。所有 JSON/CSV 的 `--output` 均写 UTF-8，
推荐直接指定它以避免 Windows 管道改变编码。目标父目录应已存在。

```powershell
python -m demo check
python -m demo export --output demo/data/plan.local.json
python -m demo homework-csv --output demo/data/homework_ch2.local.csv
python -m demo batch verification --output demo/data/verification.local.json
python -m demo batch retest_friday
python -m demo batch precheck_ch9
python -m demo batch b_followup --include-human
python -m demo batch ch9_followup
python -m demo check-run path/to/actual-run.json
```

可替换来源路径：`python -m demo --scenario path/to/scenario.json --content content/lectures check`。
`check` 或数据/数学/运行记录校验失败返回 1；参数错误返回 argparse 的 2。
`batch` 默认只有 script 学生；`--include-human` 显式加入 A/B/C 的排练提示。
D 的 `live` 条目即使给了该选项也永远不进入 batch。`export` 包括人工提示与 D 的现场
输入，T14 必须遵守各条 `operator`，不可直接循环提交 export 中的全部作答。

CSV 只包含 T17 `HomeworkRow` 支持的七列：

```text
student_id,student_name,item_id,knowledge_point_id,stem,student_answer,correct_answer
```

其中 `student_id,item_id,student_answer` 必填，其余为可选上下文。本文件不带
`is_correct`、错因、状态或教学日期；判定仍由 T17 实际执行。
T17 合并后可在 backend 目录运行：

```powershell
python -m app.ingest csv ../demo/data/homework_ch2.local.csv --teaching-date 2026-09-07
```

程序方式是 `parse_csv(csv_bytes)` 后 `await importer.import_rows(rows, date(2026, 9, 7))`。
日期由 T14 在调用边界另传，不加进 CSV。T17 的 `--teaching-date` 是实际参数名。
第二讲的原始作业 CSV 包括 A/B/C，是初始导入；三人在后续诊断、验证和复测须真人操作。

## T14 loader 协议

在进程的模块路径包含仓库根目录及 backend 后：

```python
from demo.dataset import load_dataset, validate_dataset

plan = load_dataset()  # 不写库、不调用模型
validate_dataset(plan)  # 首次消费前的数学/引用/计数检查
rows = plan.batch("verification")  # A/B/C 排除，D 本次是普通学生，允许提交
for answer in rows:
    # T14 用 student_id、stage、round 查询系统实际创建的待办和当前展示题。
    # displayed_item 来自该任务，不能拿计划 item_id 冒充系统当前题。
    message = plan.answer_for(answer.key, displayed_item)
    await orchestrator.submit_message(task_id, message)
    await orchestrator.wait_idle()
```

`answer_for` 核对实际题目的 spec 指纹、知识点、用途、考查维度，然后保留**实际展示的题目 ID**
生成既有 `MessageCreate(content, answering_item_id)`。题目不符就报错，需要真人回答或补充
经过审校的新作答规则。不要把计划答案提交给内容不同的随机生成题，也不要直接向账本插记录。
`batch` JSON 的 `message` 是原始作答结构；外层附 `key,student_id,teaching_date,round,
context_knowledge_point_id`，供 T14 选任务与推进日期，不能整包当作 HTTP 请求体。

`export` 的 `items` 是本计划涉及的实际 C2 题及本地演示题候选；不意味着 Agent 已展示了这些题。
C2 原作业本身是 `purpose=practice`，不得修改内容库用途；T17 将其作为作业提交处理。
`demo.t20.*` 是在此编写的新验证题，`source=seed` 表示人工编写候选，绝不冒称模型生成
或 C2 交付。`demo_item_c2_anchors` 将它们关联到实际 C2 原题和错因。
T14/T18 可通过正常出题入口选择这些候选，仍需通过 verify_item、抗诱错及已用指纹门槛；
不能为了命中计划绕过“三次生成失败才使用种子题”的回退规则。
如果真实 Agent 选用了不同题目，应补学生输入或调整演示计划，不能强制系统作出预期结论。

## 预跑顺序与验收口径

| 教学日期/阶段 | 操作与预期（均待真实预跑核实） |
|---|---|
| 2026-09-07 `homework_ch2` | 设第二讲目标：周五前 90% 初步掌握、最多两轮。导入 40 人，12 错，28 对。 |
| 同日 `diagnosis_ch2` | 8 人漏最内层：A、S05–S11；3 人公式代换：B、S12、S13；C 偶发失误。8 人课上集中讲，另 4 人个别处理；C 无长篇辅导。 |
| 2026-09-08 `verification` | 40 人各有正确性、抗诱错、近迁移的不同新题输入。A/B/C 由真人提交，其余 37 人由 T14。 |
| 2026-09-11 `retest_friday` | 新保持题：B 复发且命中公式代换预测，其余通过。 |
| 2026-09-14 `backtrack_ch1` | B 沿真实先修图回溯函数复合，提交 C2 顺序追问；不能据此直接确认泛化错因。 |
| 09-15、09-16 `b_followup` | B 在重新辅导后的两轮独立抗诱错题仍失败，由规划和账本据真实证据决定转交。 |
| 2026-10-26 `precheck_ch9` | T14 进入第九讲，按真实时钟规则建任务。B 这次通过；S05/S06 命中旧错，恰好 2 人复发。 |
| 10-27、10-28 `ch9_followup` | S05/S06 两轮新抗诱错题仍失败，预期分别转交。 |
| 第九讲跟进结束后 | 检查第二讲目标：37 达标，B/S05/S06 共 3 人转交。保存**真实**业务库及 checkpoint 快照。 |
| 2026-11-02 `homework_ch10_live` / `diagnosis_ch10_live` | 恢复到现场演示起点，进入第十讲；真人扮演 D 实时提交 2π/3 和圆盘面积 2π。不得提前播放成已完成结果。 |

A/B/C 原作业均为 `2\sin(3x)\cos(3x)`，仅靠此答案无法确认错因。
C2 两追问的组合区分 A（第二题仍错）、B（第一题已区分）与 C（两题正确）。
这里为 C 也准备三维验证输入，以满足状态表的严格初步掌握证据门槛；不安排长篇补课。
开发计划中“一道验证题”的简述与严格三维门槛有差异，不能只凭一道正确性题宣称初步掌握。

第九讲预检用新题 `z=sin(u)^2,u=11t`，目标仍为第二讲链式知识点，
`dimension=cross_chapter`、`context_knowledge_point_id=calc.ch9.multivar_chain`；
漏层预测为 `2sin(11t)cos(11t)`，正确为 `22sin(11t)cos(11t)`。
它有意检验**原错因**，不把第九讲新错因 ID 冒充第二讲复发。
计划提供 40 人输入作为保守候选；实际触发名单必须由 T07 的三条规则决定，不能由 T20 直接建任务。
先修中尚未取得巩固证据的节点可能使未曾出错者也进入预检。

37/3 是第九讲后、第十讲现场诊断前，对 `calc.ch2.chain_rule_multi` 目标的统计。
B 在后续预检做对不等于教师已经处理转交；如果真实 T19/规划逻辑自动解除转交，验收应报告差异，
不能补写 `escalated`。D 在第十讲失败属于另一知识点，不从已达标 37 人中硬扣一人。
12 讲热力图应显示真实覆盖率：本批 C2 没有 C3 全书证据，未评估的格子须显示未评估。

## 校验与真实结果导出

`check` 逐题调用真实 SymPy 工具，校验标准答案、预测区分度、抗诱错，按每名学生的
题目指纹拒绝重复；从两道追问的回答交集推导 8/3/1 诊断计数，从复测、预检和两轮失败
输入推导 37/3 的预期，不操作状态机。D 的 π/2、2π/3 也通过实际工具检查。

`check-run` 输入由 T14 **从真实系统读取**，顶层必须是：

```json
{
  "run_id": "实际运行标识",
  "checkpoint": "after_ch9_followup_before_ch10_live",
  "students": [],
  "evidence": [],
  "mastery": [],
  "tasks": [],
  "items": [],
  "precheck_task_ids": []
}
```

数组分别使用当前 contracts 的 Student/Evidence/Mastery/Task/Item 完整 JSON；
包含已持久化证据和任务 ID、实际生成题的 spec/答案，以及这次第九讲的真实预检任务 ID。
上面空数组仅说明结构，运行校验会拒绝。验收要求 40 人作业、12 错、诊断证据确认 8 人同因、
B 周五复发及第一讲回溯、B 预检通过、2 人旧错复发、37 人目标达标所需三维证据、
3 人转交任务的证据链。工具判定还会用题目 spec 重算。
成功只表示所提供记录一致，不能证明导出来源的真实性，亦不能代替 T21 端到端预跑。

JSON 入口逐条重新校验 SQLModel 实体，使日期、枚举和结构化题目恢复为契约类型。
验证证据必须引用验证用途的实际题目，维度须与题目一致；按学生重算 spec 指纹，
拒绝同题换 ID 或重复作答冒充不同维度。初步掌握所需维度必须来自该 Mastery 实际
引用的同一学生、知识点和任务；按 T19 的失效条件过滤复发前旧成绩，并取任务内各维度最新结果。

周五保持复测必须完整覆盖 40 人；导出的实际复测、预检任务须有对应作答，预检名单
无需强行等于计划候选名单。8/3/1 诊断从实际追问预测交集推导，B/C 的区分证据不可缺失。
三名转交对象均须有复发之后、转交之前的两轮独立验证失败，第二轮和转交任务的 round
至少为 2，失败证据须同时被转交任务和 Mastery 引用；原始作业不能代替两轮失败证据。


隔离工作区测试（不改依赖、不改 uv.lock）：

```powershell
# workdir: E:/TrueGrasp/.worktrees/T20/backend
E:/TrueGrasp/backend/.venv/Scripts/python.exe -m pytest tests/test_demo_dataset.py -q --basetemp=data/test-t20 -p no:cacheprovider
E:/TrueGrasp/backend/.venv/Scripts/python.exe -m ruff check ../demo tests/test_demo_dataset.py --config pyproject.toml
E:/TrueGrasp/backend/.venv/Scripts/python.exe -m ruff format --check ../demo tests/test_demo_dataset.py --config pyproject.toml
```
