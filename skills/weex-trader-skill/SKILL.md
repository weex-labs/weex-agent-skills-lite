---
description: Use for the WEEX Trader Lite workflow in OpenClaw: natural-language spot/futures trading, market/account queries, and fully authorized automated strategies.
name: weex-trader-skill
---

# WEEX Trader Skill — Lite Edition

This repository is the only source of truth. The skill is OpenClaw-only and keeps formal Trader trading/account behavior and complete automated-strategy authorization while omitting separate Analysis, Monitor, and Partner skills.

Resolve the installed skill root from the directory containing this `SKILL.md`, `scripts/`, and
`references/`. Run all commands from that root; do not assume the parent repository checkout is
the executable skill root.

Before every private query, order preview/confirmation, or automatic-order operation, run:

```bash
python3 scripts/weex_agent_state.py --command skill.preflight --pretty
```

Preflight is machine-only and is language-neutral. For user-facing commands, the host must pass the
latest message's detected locale with `--input-language` and may pass an explicit render locale with
`--language`. The supported locale set is declared by `references/locales/*.json`. Unknown or
undetermined input falls back to `en-US`; detection confidence below `0.8` also falls back to
`en-US`. A detected locale must match the render locale; conflicting values fail closed. Language decisions are invocation-scoped and are
never read from memory, previous assistant messages, or persisted preferences.

Stop if runtime requirements are not ready, modules are missing, environment validation fails, or `runtime.credentials.complete` is false.

## Credential boundary

The only private credential source is the complete runtime environment set `WEEX_API_KEY`, `WEEX_API_SECRET`, and `WEEX_API_PASSPHRASE`. All three must be non-empty; missing or partial sets fail closed before a WEEX request. Do not accept credentials from argv, JSON payloads, files, saved profiles, Vaults, keychains, chat, or another fallback.

Optional `WEEX_CONTRACT_API_BASE`, `WEEX_SPOT_API_BASE`, `WEEX_API_BASE`, `WEEX_API_TIMEOUT`, and `WEEX_LOCALE` remain environment-only configuration. Base URLs must pass the WEEX HTTPS allowlist.

The runtime derives an opaque account binding from the credentials and selected Spot/Futures origins. It may be stored only as a non-reversible local state key; never return or print it. Credential or origin changes invalidate pending confirmations and isolate automated authorizations from the previous environment account.

## Routing

- `scripts/weex_contract_api.py`: Live Futures market/private account/order/cancel REST.
- `scripts/weex_spot_api.py`: Spot market/private account/order/cancel REST.
- `scripts/weex_trade_guard.py`: `preview-order`, `preview-tp-sl`, `confirm-order`, `confirm-tp-sl`, `preview-cancel`, `confirm-cancel`.
- `scripts/weex_order_intent_state.py`: preview identity, TTL, environment-account and risk-signature binding.
- `scripts/weex_auto_trade.py`: stable JSON facade for strategy registration, authorization, guarded submission, reconciliation, events, snapshots, and restore.
- `scripts/weex_auto_trade_state.py`, `weex_auto_trade_amount.py`, `weex_auto_trade_runtime.py`, `weex_auto_trade_notify.py`: authorization state, conservative valuation, official facts, notification, and recovery implementation.
- `scripts/weex_message_templates.py`: locale-file-backed user-facing templates for confirmations, automatic-trading fallback, and notifications.
- `scripts/weex_user_presenter.py`: user-facing presentation boundary; domain modules do not compose localized reply text directly.
- `scripts/weex_trade_data_aggregator.py`: internal official account/market facts for guards; not a conversational analysis/replay surface.
- `scripts/weex_agent_state.py`: non-secret preflight and environment readiness summary.
- `scripts/weex_api_credentials.py`: the sole account credential loader and environment-account binding implementation.

Use only operations in `references/contract-api-definitions.json` and `references/spot-api-definitions.json`. Unknown operations and paths are rejected.

## Account queries and safe order flow

Private account queries operate on the real environment only. Every private summary starts with the returned `user_environment_prefix` and includes the real-funds warning.

1. Detect language from the latest user message, resolve the render language, then parse intent and ask only for missing/ambiguous fields; never guess quantity unit, symbol, side, or mode.
2. Call the appropriate preview command; never call a direct mutating API command from conversation.
3. Return `user_confirmation.reply_instruction` verbatim as an opaque UTF-8 string. Do not
   summarize, translate, prepend labels, remove paragraphs, reflow line breaks, or compose a
   replacement confirmation message. When present, `render_verbatim` is the host rendering
   contract and `reply_instruction_digest` can be used to verify that the displayed text was not
   changed.
4. Submit only after a later independent message exactly matches `user_confirmation.reply_text`, using the current intent ID/risk signature internally. Order fields, environment account, mode, TTL, confirmation text, and required flags remain bound.
5. Use `--confirm-live` for every mutating request. Demo modes, Demo flags and simulated endpoints are removed and fail closed before any request. A timeout or uncertain response is `REVIEW_REQUIRED`; never retry, split, or guess.

A plain market order may skip a second price comparison after exact confirmation, but all other confirmation, account, mode, TTL, and submission-uncertainty guards remain. Limit, conditional, TP/SL, and automatic-fallback paths retain fresh-fact checks.

## Automated-strategy authorization

Automatic authorization is environment-account-bound and real-trading-only. It never accepts raw credentials or an account/profile selector in its JSON requests.

- Register a stable `strategy_id`; every authorization specifies `trade_types`, symbols/all-symbols, maximum conservative U per leg, cumulative conservative U quota, and explicit `valid_hours` greater than 0 and no more than 720 hours.
- Never infer cumulative quota or validity. Show credential source, masked strategy ID, modules, symbols, limits, validity, per-order-confirmation effect, revoke action, and local trust boundary. Grant requires the exact request and `--confirm-live`.
- Only explicit official Spot/Futures order operations enter automatic submission. Fresh product, market, depth, fee, leverage, conversion, balance, scope, quota, and internal risk facts are mandatory.
- Batch reservations are atomic before any WEEX write. Explicit rejections release reservations; uncertain results remain `REVIEW_REQUIRED` and are never retried.
- Full-position TP/SL and unproven reduce-only paths remain manual. Hard constraints, state conflicts, expired/revoked authorization, unsupported operations, or incomplete facts block every write.
- Reconciliation never changes accepted conservative quota. Snapshots/restores remain owner-only local controls; restore revokes active authorizations, preserves unresolved usage, and never acts on exchange orders.
- Existing saved-profile authorizations are not migrated. After upgrading, register and explicitly authorize the current environment account.
- `submit-auto` accepts `input_language` plus an optional `language` render override for manual fallback and notification text. Unknown input resolves to `en-US`. The selected confirmation locale and word are persisted with the intent and must match exactly.
- Review the complete locale template coverage in `references/message-templates.md`; add every new user-facing template to all locale files.

Local state controls misuse/corruption; they are not identity authentication or tamper-proofing against an attacker controlling the same OS user, Agent, process environment, or API key.

## Exclusions and failure policy

Do not expose Analysis, Monitor, Partner, replay, profile analysis, deep account-risk reports, local price/PnL monitor loops, or non-OpenClaw installation. Price-threshold closes use official WEEX conditional orders.

Never send a mutating request without `--confirm-live`. Never return stale/default private data after an API error, retry an uncertain order, expose credentials/internal account IDs/raw headers, or accept a Demo mode as a real-trading equivalent.
