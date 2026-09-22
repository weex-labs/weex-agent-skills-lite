# OpenClaw Language Contract

OpenClaw must decide language from the latest user message before invoking any user-facing
WEEX command. Previous assistant messages, memory results, persisted state, and tool output are
not language evidence.

## Decision fields

```json
{
  "input_language": "ja",
  "render_language": "ja",
  "source": "detected",
  "fallback_reason": null
}
```

- `input_language` is the detected language family or `unknown`.
- `render_language` is the template locale declared by `references/locales/*.json`.
- `source` is `detected`, `explicit`, or `fallback`.
- `fallback_reason` is present for unsupported or undetermined input.
- Detection confidence below `0.8` is treated as undetermined and falls back to `en-US`.

## Resolution rules

1. `zh`, `zh-CN`, and other Chinese variants render with `zh-CN` unless a specific supported Chinese locale is detected.
2. `en`, `en-US`, and other English variants render with `en-US` unless a specific supported English locale is detected.
3. Supported locales such as Japanese, Korean, French, and Portuguese render their own locale files.
4. An unknown or undetermined language renders fixed templates with `en-US`.
5. Low-confidence detection renders fixed templates with `en-US`.
6. A caller must not override an unknown/undetermined decision with `zh-CN`; selecting `zh-CN` requires a detected Chinese input locale.
7. Preflight is machine-only and has no language parameter.

For strict single-language responses, OpenClaw should render the surrounding explanation in the
same `render_language`. If the product intentionally keeps an unsupported-language explanation,
the fixed confirmation block must remain explicitly English and its `reply_text` must be treated as
an opaque exact token.

## Confirmation binding

The language decision, render language, confirmation word, intent, account binding, TTL, and risk
signature belong to one invocation. If the language decision changes between preview and confirm,
OpenClaw must generate a new preview instead of reusing the old intent.
