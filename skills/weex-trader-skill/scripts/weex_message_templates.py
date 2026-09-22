"""Shared user-facing WEEX Trader message templates loaded from locale files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from string import Formatter
from typing import Any

from weex_language import SUPPORTED_LANGUAGES, resolve_language


LOCALE_DIR = Path(__file__).resolve().parent.parent / "references" / "locales"


def _placeholder_names(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def _load_catalog() -> dict[str, dict[str, str]]:
    catalogs: dict[str, dict[str, str]] = {}
    for locale in SUPPORTED_LANGUAGES:
        path = LOCALE_DIR / f"{locale}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid locale file for {locale}: {path}") from exc
        if payload.get("locale") != locale or not isinstance(payload.get("templates"), dict):
            raise RuntimeError(f"locale file {path} has an invalid schema")
        templates = payload["templates"]
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in templates.items()):
            raise RuntimeError(f"locale file {path} must contain string templates")
        catalogs[locale] = templates

    base_keys = set(catalogs["en-US"])
    for locale, templates in catalogs.items():
        if set(templates) != base_keys:
            missing = sorted(base_keys - set(templates))
            extra = sorted(set(templates) - base_keys)
            raise RuntimeError(
                f"locale {locale} template IDs differ from en-US; missing={missing}, extra={extra}"
            )
        for key in base_keys:
            if _placeholder_names(templates[key]) != _placeholder_names(catalogs["en-US"][key]):
                raise RuntimeError(f"locale {locale} placeholder mismatch for {key}")
    return catalogs


MESSAGE_TEMPLATES = _load_catalog()

CONFIRMATION_PROMPTS = {
    locale: {
        "reply_text": MESSAGE_TEMPLATES[locale]["confirmation.reply_text"],
        "reply_instruction": MESSAGE_TEMPLATES[locale]["confirmation.reply_instruction"],
    }
    for locale in SUPPORTED_LANGUAGES
}
AUTO_TRADE_AUTHORIZATION_HINTS = {
    locale: MESSAGE_TEMPLATES[locale]["authorization.hint"]
    for locale in SUPPORTED_LANGUAGES
}


def confirmation_instruction_digest(instruction: str) -> str:
    """Return the stable UTF-8 digest used by hosts to verify verbatim output."""
    return hashlib.sha256(instruction.encode("utf-8")).hexdigest()


def _resolved_language(language: str) -> str:
    return resolve_language(language)


def render_message(language: str, template_id: str, **values: Any) -> str:
    resolved = _resolved_language(language)
    try:
        template = MESSAGE_TEMPLATES[resolved][template_id]
    except KeyError as exc:
        raise KeyError(f"unknown message template: {template_id}") from exc
    return template.format(**values)


def build_manual_fallback_confirmation(
    language: str,
    *,
    authorization_miss: bool,
) -> dict[str, Any]:
    resolved = _resolved_language(language)
    notice_id = (
        "manual_fallback.authorization_miss_notice"
        if authorization_miss
        else "manual_fallback.generic_notice"
    )
    lines = [
        render_message(resolved, notice_id),
        "",
        render_message(resolved, "manual_fallback.order_preview"),
        "",
        render_message(resolved, "manual_fallback.confirm_line"),
    ]
    authorization_hint = ""
    if authorization_miss:
        authorization_hint = render_message(resolved, "authorization.hint")
        lines.extend(["", authorization_hint])
    return {
        "language": resolved,
        "reply_text": render_message(resolved, "confirmation.reply_text"),
        "reply_instruction": "\n".join(lines),
        "authorization_hint": authorization_hint,
        "render_verbatim": True,
        "reply_instruction_digest": confirmation_instruction_digest("\n".join(lines)),
    }


def build_notification_text(
    claim: dict[str, Any],
    language: str | None = None,
) -> tuple[str, str]:
    resolved = _resolved_language(language or claim.get("language"))
    strategy_default = render_message(resolved, "notification.strategy_default")
    strategy = str(claim.get("strategy_name") or strategy_default)
    if claim.get("kind") == "ACCEPTED_SUMMARY":
        modules = ", ".join(str(item) for item in claim.get("modules", []))
        symbols = ", ".join(str(item) for item in claim.get("symbols", []))
        return (
            render_message(resolved, "notification.accepted_summary.title", strategy=strategy),
            render_message(
                resolved,
                "notification.accepted_summary.body",
                order_count=claim.get("order_count", 0),
                modules=modules,
                symbols=symbols,
                estimated=claim.get("estimated_amount_u", "unknown"),
                remaining=claim.get("remaining_amount_u", "unknown"),
            ),
        )
    return (
        render_message(resolved, "notification.exception.title", strategy=strategy),
        render_message(
            resolved,
            "notification.exception.body",
            event_type=claim.get("event_type", "UNKNOWN_EVENT"),
        ),
    )


def environment_label(environment: dict[str, Any], language: str) -> str:
    resolved = _resolved_language(language)
    mode = str(environment.get("trading_mode") or "live").strip().lower()
    if mode != "live":
        raise ValueError("DEMO_MODE_REMOVED")
    return render_message(resolved, "environment.mode.live")


def environment_prefix(environment: dict[str, Any], language: str) -> str:
    resolved = _resolved_language(language)
    return render_message(
        resolved,
        "environment.prefix",
        mode=environment_label(environment, resolved),
    )


def environment_notice(environment: dict[str, Any], language: str) -> str:
    resolved = _resolved_language(language)
    mode = str(environment.get("trading_mode") or "live").strip().lower()
    if mode != "live":
        raise ValueError("DEMO_MODE_REMOVED")
    market = str(environment.get("market") or "trading").strip().lower()
    market_key = market if market in {"spot", "futures"} else "fallback"
    return render_message(
        resolved,
        "environment.notice.live",
        market=render_message(resolved, f"label.market.{market_key}"),
    )


def _format_value(value: Any, *, missing: str) -> str:
    if value is None or value == "":
        return missing
    return str(value)


def _market_label(market: Any, language: str) -> str:
    normalized = str(market or "").strip().lower()
    key = normalized if normalized in {"spot", "futures"} else "fallback"
    return render_message(language, f"label.market.{key}")


def _order_type_label(order_type: Any, language: str) -> str:
    normalized = str(order_type or "").strip().upper()
    key = {"MARKET": "market", "LIMIT": "limit"}.get(normalized, "fallback")
    return render_message(language, f"label.order_type.{key}")


def _order_action(order_preview: dict[str, Any], language: str) -> str:
    side = str(order_preview.get("side") or "").strip().upper()
    position_side = str(
        order_preview.get("position_side") or order_preview.get("positionSide") or ""
    ).strip().upper()
    action_key = {
        ("LONG", "BUY"): "open_long",
        ("SHORT", "SELL"): "open_short",
        ("LONG", "SELL"): "close_long",
        ("SHORT", "BUY"): "close_short",
    }.get((position_side, side))
    if action_key is None:
        action_key = {"BUY": "buy", "SELL": "sell"}.get(side, "fallback")
    return render_message(language, f"action.{action_key}")


def _is_full_position_tp_sl(order_preview: dict[str, Any]) -> bool:
    if not order_preview.get("planType"):
        return False
    quantity = order_preview.get("quantity")
    if quantity is None or str(quantity).strip() == "":
        return True
    try:
        from decimal import Decimal, InvalidOperation

        return Decimal(str(quantity)) == 0
    except (InvalidOperation, ValueError):
        return False


def format_order_summary(preview_context: dict[str, Any] | None, language: str) -> str:
    resolved = _resolved_language(language)
    order_preview = (preview_context or {}).get("order_preview")
    if not isinstance(order_preview, dict) or not order_preview:
        return render_message(resolved, "order.none")
    if order_preview.get("operation") == "cancel_order":
        target = order_preview.get("order_id") or order_preview.get("client_oid") or (
            render_message(resolved, "value.missing")
        )
        return render_message(
            resolved,
            "order.cancel",
            market=_market_label(order_preview.get("market"), resolved),
            target=target,
        )
    symbol = _format_value(
        order_preview.get("symbol"),
        missing=render_message(resolved, "value.missing"),
    )
    market = _market_label(order_preview.get("market"), resolved)
    order_type = _order_type_label(
        order_preview.get("order_type") or order_preview.get("orderType"), resolved
    )
    action = _order_action(order_preview, resolved)
    if _is_full_position_tp_sl(order_preview):
        quantity = render_message(resolved, "order.full_position")
    else:
        quantity = _format_value(
            order_preview.get("quantity") or order_preview.get("size"),
            missing=render_message(resolved, "value.missing"),
        )
    price = order_preview.get("price")
    price_text = "" if price in (None, "") else render_message(
        resolved,
        "order.price",
        value=_format_value(price, missing=render_message(resolved, "value.missing")),
    )
    trigger_price = order_preview.get("trigger_price") or order_preview.get("triggerPrice")
    trigger_text = "" if trigger_price in (None, "") else render_message(
        resolved,
        "order.trigger",
        value=_format_value(
            trigger_price,
            missing=render_message(resolved, "value.missing"),
        ),
    )
    return render_message(
        resolved,
        "order.summary",
        symbol=symbol,
        market=market,
        order_type=order_type,
        action=action,
        quantity=quantity,
        price=price_text,
        trigger=trigger_text,
    )


def market_price_warning(language: str) -> str:
    return render_message(_resolved_language(language), "price.notice")


def build_confirmation_instruction(
    language: str,
    *,
    environment: dict[str, Any],
    preview_context: dict[str, Any] | None,
    auto_trade_authorization_hint: str | None,
    reply_text: str,
    market_price_recheck_skipped: bool,
) -> tuple[str, str | None]:
    resolved = _resolved_language(language)
    mode = environment_label(environment, resolved)
    preview_mode = mode.capitalize() if resolved.startswith("en") else mode
    lines = [
        environment_prefix(environment, resolved),
        render_message(resolved, "environment.funds.real"),
        "",
        render_message(resolved, "environment.preview", mode=preview_mode),
        "",
        format_order_summary(preview_context, resolved),
    ]
    if market_price_recheck_skipped:
        lines.extend(["", market_price_warning(resolved)])
    lines.extend(
        [
            "",
            render_message(
                resolved,
                "environment.confirm.real",
                mode=mode,
                reply_text=reply_text,
            ),
        ]
    )
    if auto_trade_authorization_hint is not None:
        lines.extend(["", auto_trade_authorization_hint])
    return "\n".join(lines), None
