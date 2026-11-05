"""模块之间的契约。改动前先和柴确认（开发计划第 3 节）。

- enums：全系统共用的枚举
- values：存进 JSON 列、在 Agent 之间传递的值对象（ItemSpec、Prediction 等）和公式约定
- entities：业务实体（SQLModel 表）
- events：领域事件的 payload、推送给前端的 SSE 事件、事件总线
- agents：三个 Agent 的输入输出与接口
- verify / llm / knowledge：核验工具、模型接入、知识图谱的接口，桩实现分别在
  app.verify.tools、app.llm.client、app.knowledge.graph
- api：HTTP 请求与响应模型；路由桩在 app.api，返回 501 并标出负责实现的任务
"""
