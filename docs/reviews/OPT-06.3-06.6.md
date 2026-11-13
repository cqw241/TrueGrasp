# OPT-06.3 / OPT-06.6 接入记录：模型调用与数学工具执行优化

> 日期：2026-11-13  
> 基线：`main@57b2347`（OPT-06.1/06.2）  
> 性质：实现与确定性测试记录；真实模型体验和 OPT-06 验收项仍待人工复验。

## 1. 改动内容

1. **首轮理解与开场合并**：主动提问的 `QuestionDraft` 增加 `opening`，模型在无历史时直接写首轮回复；编排层优先使用它，为空时才调用原来的 `discuss(opening)`。答案泄露仍由 `reveals_answer` 检查。
2. **追问分类与回复合并**：`AttemptReview` 增加 `reply`，概念/澄清/其他交流的回复随意图分类一起生成；`question_turn` 优先使用 `reply`，为空才回退 `discuss`。提示请求仍走 `guide` + `HintReview`（安全审核保留）。
3. **独立转写按需执行**：`interpret` 保留 `verify_item`，把 `QuestionInterpretation` 独立转写和指纹比对移到 `confirm_spec`；只在首次实际作答、第 4 层完整解答或题意更正时执行，结果记入 `NodeReceipt.result["spec_confirmed"]`。未确认的 spec 不会用于工具判分或“核验结果”标签。
4. **SymPy 非阻塞与超时**：新增 `app/verify/execution.py`，用独立 daemon 线程执行同步数学工具并限时等待；超时/异常返回“不确定”（`equivalent=None` / `ok=None`），由模型继续评阅或解释。LLM 工具循环遇到超时/异常时把错误回传给模型，不再中断整次 `generate`。
5. **减少串行阻塞**：请求链上的 Tutor/Verifier/StudentQuestions/orchestration 异步路径改用 `AsyncMathTools`；`used_fingerprints` 的数据库读取留在主线程，只把 SymPy 指纹循环异步化；验证出题的白名单构造在异步节点内通过 `run_with_timeout` 限时执行。
6. **配置**：新增 `MATH_TOOL_TIMEOUT`（默认 8 秒）。

## 2. 模型调用次数对比

| 场景 | 改造前 | 改造后 |
|---|---|---|
| 新概念问题首轮（无 spec） | 2（`interpret` + `discuss(opening)`） | **1**（`interpret` 内含 `opening`；不跑 SymPy） |
| 新概念问题首轮（有 spec） | 3（再加独立转写） | **1**（`verify_item` 仍执行；独立转写推迟） |
| 概念追问 | 2（`review` + `discuss`） | **1**（`review.reply`） |
| 结构化问题首次作答 | 入题 2 + 作答 1 = 3 | 入题 1 + 作答 2（`review` + 按需独立转写）= 3；首条回复提前 |
| 提示请求 | 3（`review` + `guide` + `HintReview`） | 3（安全审核保留） |
| 正式验证出题 | 每维度 2（出题 + 独立审题） | 每维度 2（不变） |

确定性测试 `test_simple_questions_use_merged_replies_and_lazy_spec_confirmation` 断言了首轮 `QuestionDraft=1`、`Guidance=0`、`QuestionInterpretation=0`，概念追问 `AttemptReview=1`、`Guidance=0`，以及结构化问题首次作答时才出现一次 `QuestionInterpretation` 且 `spec_confirmed=True`。

## 3. 超时与失败语义

- 单次 SymPy 调用最长等待 `MATH_TOOL_TIMEOUT`；超时只解除事件循环阻塞，线程本身无法被强杀。
- `check_equivalent` 超时 → `equivalent=None`；`verify_item` → `ok=None`；`check_distinct` → `ok=None`。调用方按既有契约转为模型评阅或继续解释，不把超时判成错误。
- 指纹超时返回基于 `spec` JSON 的确定性哈希，避免去重集合为空。
- LLM 工具循环把超时/异常作为 `{"error": ...}` 回传模型，并提示不要重复调用。

## 4. 保留项

- 正式验证的“出题 + 独立审题”门槛。
- Tutor 生成题的 `_gate(review=True)` 独立审题。
- 提示请求的 `HintReview` 安全审核。
- “懂了”进入独立验证、证据只追加、掌握状态只由独立验证更新的既有规则。

## 5. 检查结果

```bash
cd backend
uv run pytest tests/test_llm.py tests/test_verify.py tests/test_tutor.py \
  tests/test_tutor_integration.py tests/test_orchestration.py tests/test_verifier.py \
  tests/test_verifier_ledger_integration.py tests/test_ingest.py \
  tests/test_homework_closure.py -q
# 219 passed
uv run ruff check .
uv run ruff format --check .
```

## 6. 限制

- 未用真实模型重新计时；调用次数为代码路径与确定性测试断言的对比，不是端到端耗时报告。
- 卡死的 SymPy 线程不会被强制终止；超时只保证事件循环和服务继续响应，线程会自行结束或随 daemon 线程在进程退出时放弃。
- 全局 daemon 线程未做并发上限；当前调用均在单个作答/出题链路内串行等待，未观察到线程堆积。
