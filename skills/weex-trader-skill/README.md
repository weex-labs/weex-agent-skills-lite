# weex-trader-skill (Lite / OpenClaw)

This is the only skill shipped by `weex-agent-skills-lite`.

It supports official Spot/Futures market and private account queries, guarded natural-language orders, conditional orders and TP/SL, plus the complete automated-strategy authorization facade.

Private operations require `WEEX_API_KEY`, `WEEX_API_SECRET`, and `WEEX_API_PASSPHRASE` together in the runtime environment. This package does not save credentials and does not provide profiles, Vault, argv secrets, or payload secrets. Public market commands remain credential-free.

Conversational writes must use `weex_trade_guard.py` preview/confirm. All mutating requests are live-only and require `--confirm-live`; Demo modes, Demo flags and simulated endpoints are rejected before any request. Unknown operations, partial credentials, environment-account changes, stale data, mode mismatches, and uncertain submissions fail closed.

There are no Analysis, Monitor, or Partner skills, replay/profile analysis, deep risk reports, local monitor loops, or non-OpenClaw installers.

References:

- `SKILL.md`: routing and safety contract.
- `references/script-operations.md`: CLI and automated-authorization commands.
- `references/message-templates.md`: zh/en user-facing template inventory and coverage matrix.
- `references/contract-api-definitions.json` and `references/spot-api-definitions.json`: official REST catalog.
