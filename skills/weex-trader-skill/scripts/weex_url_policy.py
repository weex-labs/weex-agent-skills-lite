#!/usr/bin/env python3
"""Shared URL policy for WEEX REST base URLs."""

from __future__ import annotations

from typing import Mapping
from urllib import request
from urllib.parse import urlparse

from weex_language import resolve_language
from weex_message_templates import render_message


ALLOWED_WEEX_BASE_DOMAINS = ("weex.com", "weex.tech")
WEEX_AUTH_HEADER_NAMES = {
    "ACCESS-KEY",
    "ACCESS-PASSPHRASE",
    "ACCESS-TIMESTAMP",
    "ACCESS-SIGN",
}


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_NO_REDIRECT_OPENER = request.build_opener(_NoRedirectHandler)


class BaseUrlPolicyError(ValueError):
    """Raised when a configured WEEX base URL violates the local safety policy."""

    def __init__(self, reason_key: str, *, label: str, host: str | None = None) -> None:
        self.reason_key = reason_key
        self.label = label
        self.host = host
        super().__init__(self.localized_message("en"))

    def localized_message(self, language: str) -> str:
        resolved_language = resolve_language(language)
        return render_message(
            resolved_language,
            f"url.{self.reason_key}",
            label=self.label,
            host=self.host,
        )


def _canonical_hostname(hostname: str) -> str:
    value = hostname.strip().rstrip(".").lower()
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def is_allowed_weex_hostname(hostname: str) -> bool:
    canonical = _canonical_hostname(hostname)
    return any(canonical == domain or canonical.endswith(f".{domain}") for domain in ALLOWED_WEEX_BASE_DOMAINS)


def validate_weex_base_url(raw_url: str, *, label: str = "base URL") -> str:
    value = str(raw_url or "").strip()
    if not value:
        raise BaseUrlPolicyError("empty", label=label)

    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise BaseUrlPolicyError("shape", label=label)
    if parsed.username or parsed.password:
        raise BaseUrlPolicyError("userinfo", label=label)
    if parsed.query or parsed.fragment:
        raise BaseUrlPolicyError("query_fragment", label=label)
    if not is_allowed_weex_hostname(parsed.hostname):
        raise BaseUrlPolicyError("host", label=label, host=parsed.hostname)

    return value.rstrip("/")


def contains_weex_auth_headers(headers: Mapping[str, object]) -> bool:
    names = {str(name).upper() for name in headers}
    return bool(names & WEEX_AUTH_HEADER_NAMES)


def open_weex_request(
    req: request.Request,
    *,
    timeout: float,
    headers: Mapping[str, object],
):
    if contains_weex_auth_headers(headers):
        return _NO_REDIRECT_OPENER.open(req, timeout=timeout)
    return request.urlopen(req, timeout=timeout)
