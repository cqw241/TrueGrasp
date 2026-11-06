# T18 独立验证 Agent

公开入口：`IndependentVerifier`、`create_verifier`、`NoValidItemError`，实现现有
`contracts.agents.Verifier`，不更改 contracts 或 OpenAPI。

## 注入

在应用的组合入口中创建模型客户端并管理其生命周期：

```python
from app.agents.verifier import create_verifier
from app.config import get_settings
from app.knowledge.graph import NxKnowledgeGraph
from app.llm.client import OpenAICompatClient
from app.orchestration import Orchestrator

knowledge = NxKnowledgeGraph.from_engine(engine)
async with OpenAICompatClient(get_settings()) as llm:
    verifier = create_verifier(llm=llm, knowledge=knowledge)
    async with Orchestrator.open(
        engine,
        checkpoint_path,
        bus,
        knowledge=knowledge,
        verifier=verifier,
        planner=planner,
        tutor=tutor,
        evidence_writer=append_evidence,  # T19 的写入函数，由组合入口提供
    ) as runtime:
        ...
```

也可向 `create_app(orchestration_dependencies={...})` 传入相同实例。
客户端必须保持打开，直到编排运行时关闭；工厂不创建网络连接、不自行关闭客户端。
`tools` 可注入，省略时采用 `SympyMathTools`。

## 隔离与证据边界

`compose` 只接受 `VerifierInput`。重新校验顶层与已确认错因字段，拒绝通过
`model_copy` 等绕过原契约校验夹带的身份/辅导历史。提示词只包含白名单及公共知识点、
策略卡评分要点；不读取学生、对话、作答或证据数据库，也不保存学生上下文。

`judge` 仅接收当前题目和当前作答，返回 `Judgement`。题目保存、曝光指纹、原答、
证据追加、事件发布由编排负责；状态迁移由 T19 负责。该模块不写证据或掌握状态。

## 出题门槛

- 按请求维度逐一出题，不重复维度；跨章节须指定知识图谱中连通的后续章节知识点。
- 每个维度最多生成 **3 次（含第一次）**，与技术文档第 6 节及现有内容起草流程一致。
  核验失败原因传给下一次生成；模型结构化输出/服务失败也消耗一次生成机会。
- 可计算题必须通过 `verify_item`。工具明确判错时不能用模型审题挽救。
- 所有通过程序门槛的候选（含数学工具验算成功的题和种子题），均另发一个无生成历史的
  独立模型审题请求，只有 `valid=True` 才可入选。审查题干与 spec 的运算、表达式、变量、
  范围是否一致，是否考查目标知识点、满足指定维度；跨章节题还须实际使用指定后续
  知识点的结构或情境，并用到目标先修知识，不能仅把旧章节计算题标成跨章节题。
  审题输入提供公共知识点/后续情境的 ID、名称、讲次及工具验算结论，不包含生成历史
  或重试原因。工具数学值结论保持权威，审题只能接纳/拒绝，不能改写答案或计算结果。
- 证明、文字解释和级数反例等工具无法验算时，上述独立审题还须检查标准答案和推理。
  可结构化的计算题不得通过省略 spec 绕开工具。
- 有预测时校验预测 ID 白名单、唯一性及 `check_distinct`；抗诱错须有可计算 spec，
  每个已确认的非 slip 错因都必须与正确答案组分离。无法工具区分时拒绝，不能模型放行。
- 解释题评分要点由已确认错因的公共策略卡提取、去重，不接受模型自拟评分要点。
- 可计算题使用数学工具规范化 spec 的指纹；纯文字题使用 NFKC、casefold、去空白后的
  题干 SHA-256。检查学生已见指纹及本批全部已选指纹，不信任种子题已有 fingerprint。
- 三次失败后，只在同知识点、同维度、未重复的种子题中选择，并重跑同一门槛。
  跨章节种子题从指定后续知识点选择，返回题仍归因到目标先修知识点。
  拷贝种子题并分配新 ID，不修改公共内容，不把正确性题改名成迁移/解释题。
- 无合格种子题则抛出 `NoValidItemError`，由编排重试/上层处理，不产生通过证据。

## 判定

可计算题先用 `spec.answer_mode` 比对标准答案和错因预测；工具已有的确定结论不得被
模型覆盖。解析失败、没有 spec 或解释题由模型评阅。解释题要求模型按策略卡逐项返回
评分，缺项/无法判断返回 `is_correct=None`，任一要点未满足则不通过。
模型返回的错因命中必须属于待判预测白名单，不能凭空生成 ID；正确作答不记错因复发。

纯工具结论标记 `JudgedBy.TOOL`；包含模型评阅时标记 `JudgedBy.MODEL`，判定理由保留
已获得的工具结果。级数审题/评阅可调用 `partial_sum`，有限部分和只提供参考，不作为
收敛性证明。无法完成的模型调用向编排抛错，不伪造未知或通过证据。

## 验证与限制

测试使用真实 SymPy 和预设的模型结构化回复，不访问远程服务。A/B 用例使用仓库真实
错因库、链式法则题目，并产生正确性/抗诱错/近迁移全部通过的判定输入；最终进入
“初步掌握”仍需由 T19 消费真实作答证据。生产模型的题干与 spec 一致性、迁移语义和
纯文字题正确性依赖模型质量；纯文字指纹不能识别同义改写。

现有内容不保证每个知识点有六维种子题，因此生成失败时可能明确报无题，不能降低门槛。
本变更不改 main/runtime，也未自动启用线上模型调用。

## 组合入口的自动装配约定

工厂签名为：

```python
create_verifier(
    *, llm: LLMClient, knowledge: KnowledgeGraph, tools: MathTools | None = None
) -> IndependentVerifier
```

直接构造签名为 `IndependentVerifier(llm, tools, knowledge)`，所有依赖同样由调用方持有。
配置自动装配时，组合入口应检查依赖字典是否**包含** `verifier` 键：
只有配置 `llm_api_key` 且未显式提供该键时创建客户端和 Verifier；显式传入 `None`
时保留未配置行为，不用真模型覆盖测试依赖。本目录不负责修改/执行该自动装配逻辑。

推荐在应用 lifespan 内以 `AsyncExitStack` 管理共享模型客户端和编排上下文：先进入
客户端上下文，再进入编排上下文；退出时先等待编排后台任务结束，再关闭客户端。
传给 Verifier 的外部客户端不会被工厂或构造器关闭，知识图谱应与编排所用实例一致；
内容更新需要由组合入口同步替换知识图谱快照。
