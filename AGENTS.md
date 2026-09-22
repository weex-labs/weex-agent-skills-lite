# WEEX Trader Lite Project Guidance

- `skills/weex-trader-skill/` is the only source-of-truth implementation layer.
- This project is OpenClaw-only and ships one skill link: `weex-trader-skill`.
- Account credentials must be read only from the complete runtime environment set: `WEEX_API_KEY`, `WEEX_API_SECRET`, and `WEEX_API_PASSPHRASE`.
- Do not add saved profiles, Vault/keychain storage, argv credentials, payload credentials, or another credential source.
- Keep the complete Trader safety flow, official Spot/Futures API definitions, preview/confirm binding, environment-account binding, and automated-strategy authorization facade.
- Never send mutating requests without the required `--confirm-live` flag.
- Use official WEEX conditional orders for price-threshold closes; do not add a local monitor task.
- Do not add Analysis, Monitor, Partner, replay, profile-analysis, deep-risk, or non-OpenClaw host support to this project.
- Never print credentials, internal environment-account IDs, or raw signed headers.
