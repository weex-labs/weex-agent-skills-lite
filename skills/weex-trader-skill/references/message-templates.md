# WEEX Trader 消息模板索引

目标架构、语言边界、机器字段与用户文案的职责划分，以 [`02-技术方案.md`](../../需求分析/language-default-removal/02-技术方案.md) 为唯一方案真源。

本文件只维护运行时模板的审查索引；运行时模板唯一实现位于 [`scripts/weex_message_templates.py`](../scripts/weex_message_templates.py)。

## 语言规则

- 支持语言由 `references/locales/*.json` 声明，包括 `en-US`、`zh-CN`、`zh-TW`、`ko`、`ja`、`vi`、`id`、`th`、`fa-IR`、`ar`、`tr`、`de`、`fr`、`it`、`es-ES`、`pt-PT`、`pl`、`ru`、`uk`、`az`、`es-419`、`es-AR`、`pt-BR`。
- 宿主只根据最新用户消息生成 locale 决策；已支持 locale 使用对应语言文件，未知或无法判断时固定文案降级为 `en-US`。
- `input_language` 与 `language`（渲染 locale）是两个不同字段；未知输入不得覆盖为 `zh-CN`。
- 英文兜底不写入全局配置。
- 显式无效语言值必须拒绝。
- intent 中的确认语言和确认词属于安全绑定，不是全局偏好。

## 模板覆盖索引

运行时 locale 文件位于 `references/locales/`。每个文件必须包含完整的 72 个模板 ID，
并与 `en-US.json` 使用完全一致的占位符集合。

| 模板命名空间 | locale 文件 | 消费方 |
| --- | --- | --- |
| `confirmation.*` | 全部 locale | Trade Guard、自动交易兜底 |
| `manual_fallback.*` | 全部 locale | 自动交易 Facade |
| `environment.*` | 全部 locale | Trade Guard 展示器 |
| `label.*` / `action.*` | 全部 locale | 订单摘要展示器 |
| `order.*` / `price.notice` | 全部 locale | Trade Guard 展示器 |
| `guard.*` | 全部 locale | Trade Guard 展示器 |
| `url.*` | 全部 locale | URL policy 展示边界 |
| `notification.*` | 全部 locale | 通知 adapter/worker |

## 维护约束

- 新增用户可见模板时，必须在所有 locale 文件中添加同名键。
- 所有 locale 必须使用相同的占位符集合。
- 机器错误码、`next_action`、内部风控诊断和 WEEX 原始错误不放入此目录。
- 内部字段只有在进入用户回复或通知正文前，才转换为模板 ID。

## 确认文案展示契约

- `user_confirmation.reply_instruction` 是唯一完整的确认展示文本；宿主必须把它当作不透明字符串原样输出。
- `render_verbatim` 为 `true` 时，禁止宿主删减、翻译、追加、重排或自行拼接确认段落。
- `reply_instruction_digest` 是完整 UTF-8 文本的 SHA-256 摘要，可用于宿主展示前后的完整性校验。
- `reply_text` 只表示后续独立确认消息必须精确匹配的确认词，不得替代 `reply_instruction` 作为预览文案。
