# T05 实现与三轮审查

按 2026-11-06 开发计划实现数学核验、极限与级数部分和。

1. 功能审查：覆盖三种等价模式、导数和偏导、不定积分、定积分、二重积分及极限。
   修复 LaTeX 解析器将 pi/e 作为符号的问题，并把严格解析失败统一转换为不确定。
2. 边界审查：结构化表达式通过 AST 白名单解析，不执行 Python；数值比较只接受有限实数，
   等价判定需要至少八个有效抽样点。积分限按内到外处理；不支持的结果返回不确定。
   修复 T04 未显式传入 slip 时缺少正确答案候选的问题。
3. 集成审查：核对 T04 草稿与内容入库调用、现有接口与 OpenAPI；保留原方法签名，
   为 ItemSpec 增加 limit_point、limit_direction 和 limit 运算，导出新版 OpenAPI。

`partial_sum(expr, variable, terms, start=1)` 返回有限项数值和，不能作为收敛性证明。
极限目前返回有限显式结果；无限或不可计算结果交给模型评阅。数值抽样是近似检验。

验证：`pytest`、`ruff check .`、`ruff format --check .`、`python -m app.openapi --check`。
