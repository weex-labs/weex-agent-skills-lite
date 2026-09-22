"""User-facing presentation boundary for the WEEX Trader skill."""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from weex_language import LanguageContext, resolve_language, resolve_language_context
from weex_message_templates import (
    AUTO_TRADE_AUTHORIZATION_HINTS,
    CONFIRMATION_PROMPTS,
    build_confirmation_instruction,
    build_manual_fallback_confirmation,
    build_notification_text,
    confirmation_instruction_digest,
    environment_label,
    environment_notice,
    environment_prefix,
    render_message,
)


@dataclass(frozen=True)
class UserMessage:
    language: str
    template_id: str
    text: str


def present_user_message(
    language: str | LanguageContext,
    template_id: str,
    **values: Any,
) -> UserMessage:
    context = (
        language
        if isinstance(language, LanguageContext)
        else resolve_language_context(language)
    )
    return UserMessage(
        language=context.language,
        template_id=template_id,
        text=render_message(context.language, template_id, **values),
    )


def present_confirmation(
    language: str,
    *,
    environment: dict[str, Any],
    preview_context: dict[str, Any] | None,
    authorization_hint: str | None,
    reply_text: str,
    market_price_recheck_skipped: bool,
) -> tuple[str, str | None]:
    """Render the complete user-facing confirmation instruction."""
    return build_confirmation_instruction(
        language,
        environment=environment,
        preview_context=preview_context,
        auto_trade_authorization_hint=authorization_hint,
        reply_text=reply_text,
        market_price_recheck_skipped=market_price_recheck_skipped,
    )


def present_user_confirmation(
    language: str | LanguageContext,
    *,
    environment: dict[str, Any] | None = None,
    preview_context: dict[str, Any] | None = None,
    include_auto_trade_authorization_hint: bool = False,
    market_price_recheck_skipped: bool = False,
) -> dict[str, Any]:
    context = (
        language
        if isinstance(language, LanguageContext)
        else resolve_language_context(language)
    )
    resolved = context.language
    prompt = CONFIRMATION_PROMPTS[resolved]
    hint = (
        AUTO_TRADE_AUTHORIZATION_HINTS[resolved]
        if include_auto_trade_authorization_hint
        else None
    )
    instruction = prompt["reply_instruction"]
    if environment is not None:
        instruction, _ = present_confirmation(
            resolved,
            environment=environment,
            preview_context=preview_context,
            authorization_hint=hint,
            reply_text=prompt["reply_text"],
            market_price_recheck_skipped=market_price_recheck_skipped,
        )
    elif hint is not None:
        instruction = "\n\n".join((instruction, hint))
    result = {
        "language": resolved,
        "language_source": context.source,
        "reply_text": prompt["reply_text"],
        "reply_instruction": instruction,
        "render_verbatim": True,
        "reply_instruction_digest": confirmation_instruction_digest(instruction),
    }
    if context.input_language is not None:
        result["input_language"] = context.input_language
    if context.fallback_reason is not None:
        result["fallback_reason"] = context.fallback_reason
    return result


def present_manual_fallback(
    language: str,
    *,
    authorization_miss: bool,
) -> dict[str, str]:
    return build_manual_fallback_confirmation(
        language,
        authorization_miss=authorization_miss,
    )


def present_notification(claim: dict[str, Any], language: str) -> tuple[str, str]:
    return build_notification_text(claim, language=language)


def present_environment_notice(environment: dict[str, Any], language: str) -> str:
    return environment_notice(environment, language)


def present_environment_label(environment: dict[str, Any], language: str) -> str:
    return environment_label(environment, language)


def present_environment_prefix(environment: dict[str, Any], language: str) -> str:
    return environment_prefix(environment, language)


def present_message(language: str, template_id: str, **values: Any) -> str:
    return render_message(language, template_id, **values)


__all__ = [
    "present_confirmation",
    "present_environment_notice",
    "present_environment_label",
    "present_environment_prefix",
    "present_manual_fallback",
    "present_message",
    "present_notification",
    "present_user_confirmation",
    "present_user_message",
    "UserMessage",
]
