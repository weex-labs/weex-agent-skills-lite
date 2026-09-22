# weex-agent-skills-lite

Published project: [weex-agent-skills-lite](https://github.com/enzo108216/weex-agent-skills-lite)

OpenClaw-only WEEX Trader Lite skill. It keeps the complete Trader trading, market/account, preview/confirm, and automated-strategy authorization behavior while shipping no separate Analysis, Monitor, or Partner skills.

## Install or update in OpenClaw

From a development checkout:

```bash
bash skills/weex-trader-skill/scripts/update_openclaw_skills.sh --dev
```

For a release, run without `--dev` and set `WEEX_OPENCLAW_APPROVED_COMMIT` to the approved immutable commit. The updater validates the checkout, refreshes only `weex-trader-skill`, runs OpenClaw checks, and restores the previous link on failure.

## Capabilities

- Public Spot/Futures prices, K-lines, depth, and funding rates.
- Private balances, available/frozen amounts, positions, orders, and fills for the real environment with explicit live-trading prefix and real-funds warning.
- Natural-language orders with missing-field questions, product validation, preview, exact independent confirmation, cancellation, official conditional orders, TP/SL, status, and fills.
- Complete automated-strategy authorization: stable strategy IDs, module/symbol scope, conservative per-leg and cumulative quotas, validity, revocation, audit, guarded submission, reconciliation, snapshots, and restore.

## Credentials and safety

Private operations read credentials only from the OpenClaw runtime environment. Configure all three together:

```text
WEEX_API_KEY
WEEX_API_SECRET
WEEX_API_PASSPHRASE
```

The project provides no saved-profile, Vault, argv, or JSON-payload credential path. A partial set fails closed. The skill derives a non-secret local account binding so changing credentials or API origins cannot reuse another account's pending confirmation or automated-trading authorization.

Every conversational order uses a guard preview and the exact confirmation text from the latest preview. All writes are live-only and require `--confirm-live`; Demo modes and simulated endpoints are not supported. Credentials, internal account IDs, and raw signed headers are never returned.

OpenClaw language routing uses the latest user message only: pass `--input-language` to user-facing commands. Supported locale files render their own fixed text; unknown or undetermined languages fall back to `en-US`. Preflight is machine-only and language-neutral.

This project excludes replay/profile analysis, deep account-risk interpretation, PnL monitor tasks, local automatic-close loops, Partner/referral APIs, and non-OpenClaw installers. Price-threshold closes use official WEEX conditional orders.

See [`skills/weex-trader-skill/SKILL.md`](skills/weex-trader-skill/SKILL.md) and [`script-operations.md`](skills/weex-trader-skill/references/script-operations.md).
