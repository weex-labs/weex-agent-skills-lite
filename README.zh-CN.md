# weex-agent-skills-lite

项目发布地址：[weex-agent-skills-lite](https://github.com/enzo108216/weex-agent-skills-lite)

仅支持 OpenClaw 的 WEEX Trader Lite 专用项目。保留正式 Trader 的现货/合约交易、行情/账户查询和完整自动交易授权，不安装 Analysis、Monitor、Partner 等独立 Skill。

## 在 OpenClaw 中安装或更新

在本项目 checkout 中执行：

```bash
bash skills/weex-trader-skill/scripts/update_openclaw_skills.sh --dev
```

发布版本执行同一脚本但去掉 `--dev`，并设置 `WEEX_OPENCLAW_APPROVED_COMMIT` 为已发布 commit。更新器会校验 Git checkout，仅刷新 `weex-trader-skill` 链接，运行 OpenClaw 检查；校验失败会恢复原链接。本项目不提供 Codex、Claude Code、Cursor、GitHub Copilot 或其他宿主安装器。

## 支持范围

- 自然语言现货/合约交易：缺参追问、产品规则校验、预览、独立确认、撤单、官方条件单、止盈止损、订单状态、仓位和成交查询。
- Lite 的普通确认只展示订单与环境信息并要求二次确认，不向用户输出风险分析提示；自动交易授权仍保留，且使用固定授权提示词。
- 现货/合约公开行情：价格、K 线、深度、资金费率。
- 现货/合约真实盘私有账户：余额、可用/冻结金额、仓位、订单和成交；必须显示真实盘前缀和真实资金提醒。
- 完整自动交易授权：稳定策略 ID、现货/合约和交易对范围、单腿/累计保守额度、有效期、撤销、审计、受保护提交、对账、快照和恢复。

## 明确排除

不提供 replay/画像分析、深度账户风险解释、PnL 监控任务、本地自动平仓循环、Partner/合伙人查询，也不支持非 OpenClaw 宿主。价格阈值平仓使用 WEEX 官方条件单，不创建本地 monitor。

## 凭据与安全

账户密钥仅由 OpenClaw 运行时注入，必须同时配置完整的 `WEEX_API_KEY`、`WEEX_API_SECRET`、`WEEX_API_PASSPHRASE`；缺失或部分配置会直接拒绝私有操作。本项目不提供 saved profile、Vault、命令行参数或 JSON payload 凭据入口。不得把秘密放在命令行参数或聊天中。

自然语言订单必须先 `preview-order`，并将后续独立消息中的最新确认文本原样传给 confirm 命令的 `--user-reply`；所有写入均为真实盘并需要 `--confirm-live`，模拟盘、Demo 旗标和模拟端点均不支持。环境凭据或 API origin 变化后，旧确认和旧自动交易授权不可复用。

OpenClaw 只根据最新用户消息决定 locale：用户侧命令传入 `--input-language`；已支持的 locale 使用对应语言文件，未知或无法判断的语种固定文案统一降级为 `en-US`。Preflight 只输出机器状态，不参与用户语种渲染。

详细路由和自动授权命令见 [`skills/weex-trader-skill/SKILL.md`](skills/weex-trader-skill/SKILL.md) 与 [`skills/weex-trader-skill/references/script-operations.md`](skills/weex-trader-skill/references/script-operations.md)。
