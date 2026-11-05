# T06 模型接入与三轮审查

统一入口为 `OpenAICompatClient(Settings(...))`，所有 Agent 共用模型配置；每次调用的历史独立。
使用 `async with OpenAICompatClient(settings) as client` 管理 SDK 生命周期。
`generate(messages, OutputModel, math_tools(SympyMathTools()))` 提供三个数学核验工具。
工具参数先经 Pydantic 校验，未知工具或非法参数返回工具错误供模型修正，handler 异常向上传播。
`stream(messages)` 返回文本异步迭代器，取消/异常时释放流；提前退出时调用者应关闭迭代器。

`transcribe_answer(client, image_urls)` 支持多张 HTTP 或 data URL 图片，返回 LaTeX、
识别疑问 `doubts` 和强制为真的 `requires_confirmation`。调用方须让学生确认后再核验。
`PromptTemplates` 用 `$variable` 替换，JSON 花括号不需转义；缺少变量会明确报错。

环境配置见 `backend/.env.example`。默认 DeepSeek 使用 `json_object`，系统提示附完整 JSON Schema，
本地验证并在失败时重试。支持 `LLM_RESPONSE_FORMAT=json_schema` 的服务可改用该格式。
参考：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、
[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。

1. 功能审查：核对模型/base URL/key 配置、结构化输出、工具调用往返、模板和图片转写。
2. 边界审查：验证重试和工具轮次都有上限；拒绝/截断报错，工具参数非法不执行 handler；
   图片确认标记由程序强制；日志仅记录型号、耗时、用量及状态，不包含图片、密钥或对话。
3. 集成审查：通过真实 SDK 和 HTTP MockTransport 检验请求及 SSE；检查三角色均走同一层且
   不修改传入历史。发现无 stop 的流可能被当成成功，已增加完成标记校验及回归用例。

验证：完整 pytest、ruff、OpenAPI 检查。HTTP 模拟覆盖客户端协议，不需要真实密钥；
实际供应商的图片识别质量与额度需在配置有效密钥后联调。
