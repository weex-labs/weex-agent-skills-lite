#!/usr/bin/env python3
"""WEEX Contract REST API helper.

- Endpoint definitions loaded from references/contract-api-definitions.json
- Private auth only from standard runtime environment variables
- Supports generic endpoint calls and deterministic convenience commands
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error, parse, request

from weex_agent_state import (
    RuntimePreflightError,
    ensure_private_runtime_ready,
    refresh_agent_records,
    validate_runtime_environment,
)
from weex_api_credentials import load_environment_account
from weex_url_policy import BaseUrlPolicyError, open_weex_request, validate_weex_base_url

DEFAULT_BASE_URL = "https://api-contract.weex.com"
DEFAULT_LOCALE = "en-US"
DEFAULT_TIMEOUT = 15.0
DEFAULT_TRADING_MODE = "live"
TRADING_MODES = ("live",)
DAY_MS = 24 * 60 * 60 * 1000
GET_BODY_UNSUPPORTED_MESSAGE = (
    "GET requests do not accept --body. Pass request fields with --query instead."
)
PRIVATE_CREDENTIALS_REQUIRED_MESSAGE = (
    "Private commands require WEEX_API_KEY, WEEX_API_SECRET, and WEEX_API_PASSPHRASE "
    "in the runtime environment."
)


@dataclass(frozen=True)
class Endpoint:
    key: str
    group: str
    title: str
    method: str
    path: str
    auth: bool
    mutating: bool
    doc_url: str
    permission: str = ""
    request_transport: str = "query"
    query_fields: tuple[str, ...] = ()
    body_fields: tuple[str, ...] = ()
    required_query_fields: tuple[str, ...] = ()
    required_body_fields: tuple[str, ...] = ()


def load_endpoint_map() -> Dict[str, Endpoint]:
    refs = Path(__file__).resolve().parent.parent / "references" / "contract-api-definitions.json"
    obj = json.loads(refs.read_text(encoding="utf-8"))
    endpoint_map: Dict[str, Endpoint] = {}
    for d in obj.get("definitions", []):
        if str(d.get("key") or "").startswith("sim.") or str(d.get("path") or "").startswith("/capi/v3/sim/"):
            continue
        method = d.get("method", "GET").upper()
        auth = bool(d.get("requires_auth", False))
        permission = str(d.get("permission", ""))
        ep = Endpoint(
            key=d["key"],
            group=d.get("category", ""),
            title=d.get("title", ""),
            method=method,
            path=d.get("path", ""),
            auth=auth,
            mutating=auth and permission == "TRADE",
            doc_url=d.get("doc_url", ""),
            permission=permission,
            request_transport=str(d.get("request_transport", "query")),
            query_fields=tuple(str(value) for value in d.get("query_fields", [])),
            body_fields=tuple(str(value) for value in d.get("body_fields", [])),
            required_query_fields=tuple(
                str(item["name"])
                for item in d.get("request_params", [])
                if isinstance(item, dict)
                and item.get("name") in d.get("query_fields", [])
                and str(item.get("required", "")).strip().lower() in {"yes", "required"}
            ),
            required_body_fields=tuple(
                str(item["name"])
                for item in d.get("request_params", [])
                if isinstance(item, dict)
                and item.get("name") in d.get("body_fields", [])
                and str(item.get("required", "")).strip().lower() in {"yes", "required"}
            ),
        )
        endpoint_map[ep.key] = ep
    return endpoint_map


ENDPOINTS = load_endpoint_map()


def parse_json_arg(raw: str, arg_name: str) -> Dict[str, Any]:
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("@"):
        raise SystemExit(
            f"{arg_name} no longer accepts @file input. Pass a JSON object string directly."
        )
    payload = raw
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON for {arg_name}: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SystemExit(f"{arg_name} must be a JSON object")
    return parsed


def compact_json(value: Optional[Dict[str, Any]]) -> str:
    if not value:
        return ""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _business_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return True
    error_code = payload.get("errorCode")
    if error_code not in (None, "", 0, "0", "00000"):
        return True
    code = payload.get("code")
    return code not in (None, "", 0, "0", "00000", "SUCCESS")


class WeexContractClient:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        locale: str,
        api_key: Optional[str],
        api_secret: Optional[str],
        api_passphrase: Optional[str],
        user_agent: str = "weex-trader-skill-contract/1.0",
    ) -> None:
        try:
            self.base_url = validate_weex_base_url(base_url, label="contract base URL")
        except BaseUrlPolicyError as exc:
            raise SystemExit(str(exc)) from exc
        self.timeout = timeout
        self.locale = locale
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.user_agent = user_agent

    def _require_auth(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("API Key")
        if not self.api_secret:
            missing.append("Secret Key")
        if not self.api_passphrase:
            missing.append("Passphrase")
        if missing:
            raise SystemExit(
                PRIVATE_CREDENTIALS_REQUIRED_MESSAGE + " Missing: " + ", ".join(missing)
            )

    def _sign(self, timestamp_ms: str, method: str, path: str, query_string: str, body_str: str) -> str:
        # Per WEEX docs, message = timestamp + method + requestPath + (?queryString) + body
        message = f"{timestamp_ms}{method}{path}"
        if query_string:
            message += f"?{query_string}"
        message += body_str
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def prepare_request(
        self,
        endpoint: Endpoint,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        method = endpoint.method.upper()
        q = query or {}
        b = body or {}
        self._validate_payload_fields(endpoint, q, b)
        if method == "GET" and b:
            raise SystemExit(GET_BODY_UNSUPPORTED_MESSAGE)
        if endpoint.request_transport == "query" and b:
            raise SystemExit(
                f"request_transport_mismatch: {endpoint.key} requires query fields; "
                "pass them with --query and leave --body empty"
            )
        if endpoint.request_transport == "body" and q:
            raise SystemExit(
                f"request_transport_mismatch: {endpoint.key} requires a JSON body; "
                "pass fields with --body and leave --query empty"
            )
        query_string = parse.urlencode(q, doseq=True)
        body_str = compact_json(b)

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "locale": self.locale,
            "User-Agent": self.user_agent,
        }

        if endpoint.auth:
            self._require_auth()
            timestamp_ms = str(int(time.time() * 1000))
            sign = self._sign(timestamp_ms, method, endpoint.path, query_string, body_str)
            headers.update(
                {
                    "ACCESS-KEY": self.api_key,
                    "ACCESS-PASSPHRASE": self.api_passphrase,
                    "ACCESS-TIMESTAMP": timestamp_ms,
                    "ACCESS-SIGN": sign,
                }
            )

        url = f"{self.base_url}{endpoint.path}"
        if query_string:
            url = f"{url}?{query_string}"

        data = body_str.encode("utf-8") if body_str and method != "GET" else None

        return {
            "method": method,
            "url": url,
            "headers": headers,
            "data": data,
            "query": q,
            "body": b,
        }

    @staticmethod
    def _validate_payload_fields(
        endpoint: Endpoint,
        query: Dict[str, Any],
        body: Dict[str, Any],
    ) -> None:
        unknown_query = sorted(set(query) - set(endpoint.query_fields))
        unknown_body = sorted(set(body) - set(endpoint.body_fields))
        if unknown_query:
            raise SystemExit(
                f"unknown query fields for {endpoint.key}: {', '.join(unknown_query)}"
            )
        if unknown_body:
            raise SystemExit(
                f"unknown body fields for {endpoint.key}: {', '.join(unknown_body)}"
            )
        missing_query = [
            field
            for field in endpoint.required_query_fields
            if query.get(field) in (None, "")
        ]
        missing_body = [
            field
            for field in endpoint.required_body_fields
            if body.get(field) in (None, "")
        ]
        if missing_query or missing_body:
            missing = ", ".join([*missing_query, *missing_body])
            raise SystemExit(f"missing required request fields for {endpoint.key}: {missing}")

    def send(self, prepared: Dict[str, Any]) -> Dict[str, Any]:
        req = request.Request(
            url=prepared["url"],
            method=prepared["method"],
            data=prepared["data"],
            headers=prepared["headers"],
        )
        try:
            with open_weex_request(req, timeout=self.timeout, headers=prepared["headers"]) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"raw": raw}
                if _business_error(payload):
                    return {"ok": False, "status": resp.status, "error": payload}
                return {"ok": True, "status": resp.status, "data": payload}
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw}
            return {
                "ok": False,
                "status": exc.code,
                "error": payload,
            }
        except error.URLError as exc:
            return {
                "ok": False,
                "status": None,
                "error": {"message": str(exc)},
            }


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    result = dict(headers)
    if "ACCESS-KEY" in result:
        result["ACCESS-KEY"] = "***"
    if "ACCESS-PASSPHRASE" in result:
        result["ACCESS-PASSPHRASE"] = "***"
    if "ACCESS-SIGN" in result:
        result["ACCESS-SIGN"] = "***"
    return result


def output_json(payload: Dict[str, Any], pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(json.dumps(payload, ensure_ascii=False))


def normalize_trading_mode(raw: str | None, *, required: bool = False) -> str:
    if raw in (None, ""):
        if required:
            raise SystemExit("trading_mode_required: choose live for private contract operations")
        return DEFAULT_TRADING_MODE
    mode = str(raw).strip().lower()
    if mode == "demo":
        raise SystemExit("DEMO_MODE_REMOVED: Futures demo trading is no longer supported")
    if mode not in TRADING_MODES:
        raise SystemExit(f"invalid_trading_mode: expected one of {', '.join(TRADING_MODES)}")
    return mode


def environment_for_mode(trading_mode: str) -> Dict[str, Any]:
    normalize_trading_mode(trading_mode)
    environment = {
        "trading_mode": "live",
        "label": "live",
        "market": "futures",
        "uses_real_funds": True,
    }
    return environment


def add_environment_context(payload: Dict[str, Any], environment: Dict[str, Any]) -> None:
    payload["environment"] = environment


def validate_endpoint_trading_mode(endpoint: Endpoint, trading_mode: str) -> str:
    mode = normalize_trading_mode(trading_mode)
    return mode


def validate_confirm_flags(
    endpoint: Endpoint,
    trading_mode: str,
    dry_run: bool,
    confirm_live: bool,
) -> None:
    if not endpoint.mutating or dry_run:
        return
    if not confirm_live:
        raise SystemExit(
            f"confirm_flag_mode_mismatch: live mutating request for {endpoint.key} requires --confirm-live"
        )


def _upper_body_value(body: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = body.get(key)
        if value is not None:
            return str(value).strip().upper()
    return ""


def _is_directional_close_like_order(body: Dict[str, Any]) -> bool:
    side = _upper_body_value(body, "side")
    position_side = _upper_body_value(body, "positionSide", "position_side")
    return (position_side == "LONG" and side == "SELL") or (
        position_side == "SHORT" and side == "BUY"
    )


def validate_pending_order_routing(endpoint: Endpoint, body: Dict[str, Any]) -> None:
    if endpoint.key != "transaction.place_pending_order":
        return
    if _is_directional_close_like_order(body):
        raise SystemExit(
            "pending_close_requires_tp_sl: price-threshold close requests must use "
            "preview-tp-sl/confirm-tp-sl (placeTpSlOrder) because generic "
            "place_pending_order is not guaranteed reduce-only"
        )


def validate_endpoint_constraints(
    endpoint: Endpoint,
    query: Dict[str, Any],
    body: Dict[str, Any],
) -> None:
    if endpoint.key == "market.get_depth_data" and "limit" in query:
        try:
            depth_limit = int(query["limit"])
        except (TypeError, ValueError) as exc:
            raise SystemExit("limit must be one of the documented values: 15 or 200") from exc
        if depth_limit not in {15, 200}:
            raise SystemExit("limit must be one of the documented values: 15 or 200")

    if endpoint.key == "transaction.place_orders_batch":
        batch_orders = body.get("batchOrders")
        if batch_orders is not None:
            if not isinstance(batch_orders, list):
                raise SystemExit("batchOrders must be an array")
            if len(batch_orders) > 5:
                raise SystemExit("batchOrders accepts at most 5 orders per request")

    if endpoint.key == "account.update_leverage_trade":
        leverage_fields = (
            "crossLeverage",
            "isolatedLongLeverage",
            "isolatedShortLeverage",
        )
        if not any(body.get(field) not in (None, "") for field in leverage_fields):
            raise SystemExit(
                "At least one leverage field is required: crossLeverage, "
                "isolatedLongLeverage, or isolatedShortLeverage"
            )

    if endpoint.key in {
        "market.get_funding_rate_history",
        "transaction.get_trade_details",
    }:
        start_raw = query.get("startTime")
        end_raw = query.get("endTime")
        parsed_times: Dict[str, int] = {}
        for field, raw_value in (("startTime", start_raw), ("endTime", end_raw)):
            if raw_value is None:
                continue
            try:
                parsed_times[field] = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"{field} must be a Unix millisecond integer") from exc
        if start_raw is not None and end_raw is not None:
            start_ms = parsed_times["startTime"]
            end_ms = parsed_times["endTime"]
            if end_ms < start_ms:
                raise SystemExit("endTime must be greater than or equal to startTime")
            if end_ms - start_ms > 7 * DAY_MS:
                raise SystemExit("startTime and endTime must span no more than 7 days")
        if endpoint.key == "transaction.get_trade_details":
            oldest_allowed = int(time.time() * 1000) - 365 * DAY_MS
            if any(value < oldest_allowed for value in parsed_times.values()):
                raise SystemExit("Trade details can only be queried within the past 365 days")


def execute_endpoint(
    client: WeexContractClient,
    endpoint_key: str,
    query: Dict[str, Any],
    body: Dict[str, Any],
    dry_run: bool,
    confirm_live: bool,
    trading_mode: str | None,
    pretty: bool,
) -> int:
    if trading_mode in (None, "") and ENDPOINTS[endpoint_key].auth:
        raise SystemExit("trading_mode_required: choose live for private contract operations")
    effective_mode = normalize_trading_mode(trading_mode)
    code, payload = execute_endpoint_payload(
        client=client,
        endpoint_key=endpoint_key,
        query=query,
        body=body,
        dry_run=dry_run,
        confirm_live=confirm_live,
        trading_mode=effective_mode,
    )
    output_json(payload, pretty)
    return code


def execute_endpoint_payload(
    *,
    client: WeexContractClient,
    endpoint_key: str,
    query: Dict[str, Any],
    body: Dict[str, Any],
    dry_run: bool,
    confirm_live: bool,
    trading_mode: str | None,
) -> tuple[int, Dict[str, Any]]:
    endpoint = ENDPOINTS[endpoint_key]
    if trading_mode in (None, "") and endpoint.auth:
        raise SystemExit("trading_mode_required: choose live for private contract operations")
    mode = validate_endpoint_trading_mode(endpoint, normalize_trading_mode(trading_mode))
    validate_confirm_flags(endpoint, mode, dry_run, confirm_live)
    validate_pending_order_routing(endpoint, body)
    validate_endpoint_constraints(endpoint, query, body)

    prepared = client.prepare_request(
        endpoint,
        query=query,
        body=body,
    )
    environment = environment_for_mode(mode) if endpoint.auth else None
    if dry_run:
        preview = {
            "dry_run": True,
            "endpoint": endpoint.key,
            "method": prepared["method"],
            "url": prepared["url"],
            "headers": sanitize_headers(prepared["headers"]),
            "query": query,
            "body": body,
        }
        if environment is not None:
            add_environment_context(preview, environment)
        return 0, preview

    response = client.send(prepared)
    payload = {
        "endpoint": endpoint.key,
        "method": endpoint.method,
        "path": endpoint.path,
        "status": response.get("status"),
        "ok": response.get("ok"),
        "result": response.get("data") if response.get("ok") else response.get("error"),
    }
    if environment is not None:
        add_environment_context(payload, environment)
    return (0 if response.get("ok") else 1), payload


def generate_client_oid() -> str:
    return f"codex-{int(time.time() * 1000)}-{secrets.token_hex(3)}"


def find_endpoint_key_by_doc_suffix(doc_suffix: str) -> str:
    target = f"/{doc_suffix}"
    matches = sorted(
        endpoint.key
        for endpoint in ENDPOINTS.values()
        if endpoint.doc_url.endswith(target)
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(
            f"Ambiguous endpoint doc suffix {doc_suffix}; matches: {', '.join(matches)}"
        )
    raise SystemExit(f"Unable to find endpoint with doc suffix {doc_suffix}")


def normalize_contract_trade_symbol(symbol: str) -> str:
    s = symbol.strip().upper().replace("-", "").replace("/", "").replace(" ", "").replace("_", "")
    if s.startswith("CMT") and s.endswith("USDT"):
        s = s[3:]
    if s.endswith("USDT") and len(s) > 4:
        return s
    raise SystemExit(f"Unsupported symbol format: {symbol}. Expected like ETHUSDT.")


def normalize_contract_symbol(symbol: str) -> str:
    return normalize_contract_trade_symbol(symbol)


def command_requires_auth(args: argparse.Namespace) -> bool:
    if args.command == "call":
        return ENDPOINTS[args.endpoint].auth
    return args.command in {"place-order", "cancel-order"}


def cmd_list_endpoints(args: argparse.Namespace) -> int:
    rows = []
    for endpoint in sorted(ENDPOINTS.values(), key=lambda e: (e.group, e.key)):
        if args.group and endpoint.group != args.group:
            continue
        rows.append(
            {
                "key": endpoint.key,
                "group": endpoint.group,
                "method": endpoint.method,
                "path": endpoint.path,
                "auth": endpoint.auth,
                "mutating": endpoint.mutating,
                "permission": endpoint.permission,
                "request_transport": endpoint.request_transport,
                "query_fields": list(endpoint.query_fields),
                "body_fields": list(endpoint.body_fields),
                "doc_url": endpoint.doc_url,
            }
        )
    output_json({"count": len(rows), "endpoints": rows}, args.pretty)
    return 0


def cmd_call(args: argparse.Namespace, client: WeexContractClient) -> int:
    query = parse_json_arg(args.query, "--query")
    body = parse_json_arg(args.body, "--body")
    return execute_endpoint(
        client=client,
        endpoint_key=args.endpoint,
        query=query,
        body=body,
        dry_run=args.dry_run,
        confirm_live=args.confirm_live,
        trading_mode=args.trading_mode,
        pretty=args.pretty,
    )


def cmd_place_order(args: argparse.Namespace, client: WeexContractClient) -> int:
    mode = normalize_trading_mode(args.trading_mode, required=True)
    body_symbol = normalize_contract_trade_symbol(args.symbol)
    body: Dict[str, Any] = {
        "symbol": body_symbol,
        "side": args.side.upper(),
        "positionSide": args.position_side.upper(),
        "type": args.order_type.upper(),
        "quantity": args.quantity,
        "newClientOrderId": args.new_client_order_id or generate_client_oid(),
    }
    if args.price is not None:
        body["price"] = args.price
    if args.time_in_force is not None:
        body["timeInForce"] = args.time_in_force.upper()
    if args.tp_trigger_price is not None:
        body["tpTriggerPrice"] = args.tp_trigger_price
    if args.sl_trigger_price is not None:
        body["slTriggerPrice"] = args.sl_trigger_price
    if args.tp_working_type is not None:
        body["TpWorkingType"] = args.tp_working_type.upper()
    if args.sl_working_type is not None:
        body["SlWorkingType"] = args.sl_working_type.upper()

    if body["type"] == "LIMIT":
        if "price" not in body:
            raise SystemExit("price is required when type=LIMIT")
        if "timeInForce" not in body:
            raise SystemExit("time-in-force is required when type=LIMIT")
    else:
        if "price" in body:
            raise SystemExit("price must be omitted when type=MARKET")
        if "timeInForce" in body:
            raise SystemExit("time-in-force must be omitted when type=MARKET")

    endpoint_key = "transaction.place_order"

    return execute_endpoint(
        client=client,
        endpoint_key=endpoint_key,
        query={},
        body=body,
        dry_run=args.dry_run,
        confirm_live=args.confirm_live,
        trading_mode=args.trading_mode,
        pretty=args.pretty,
    )


def cmd_cancel_order(args: argparse.Namespace, client: WeexContractClient) -> int:
    query: Dict[str, Any] = {}
    if args.order_id:
        query["orderId"] = args.order_id
    if args.client_oid:
        query["origClientOrderId"] = args.client_oid
    if not query:
        raise SystemExit("Provide at least one of --order-id or --client-oid")

    return execute_endpoint(
        client=client,
        endpoint_key=find_endpoint_key_by_doc_suffix("CancelOrder"),
        query=query,
        body={},
        dry_run=args.dry_run,
        confirm_live=args.confirm_live,
        trading_mode=DEFAULT_TRADING_MODE,
        pretty=args.pretty,
    )


def cmd_ticker(args: argparse.Namespace, client: WeexContractClient) -> int:
    return execute_endpoint(
        client=client,
        endpoint_key=find_endpoint_key_by_doc_suffix("GetSymbolPrice"),
        query={"symbol": normalize_contract_symbol(args.symbol)},
        body={},
        dry_run=False,
        confirm_live=False,
        trading_mode=DEFAULT_TRADING_MODE,
        pretty=args.pretty,
    )


def cmd_poll_ticker(args: argparse.Namespace, client: WeexContractClient) -> int:
    run_count = 0
    while True:
        run_count += 1
        code = execute_endpoint(
            client=client,
            endpoint_key=find_endpoint_key_by_doc_suffix("GetSymbolPrice"),
            query={"symbol": normalize_contract_symbol(args.symbol)},
            body={},
            dry_run=False,
            confirm_live=False,
            trading_mode=DEFAULT_TRADING_MODE,
            pretty=args.pretty,
        )
        if code != 0:
            return code
        if args.count > 0 and run_count >= args.count:
            return 0
        time.sleep(args.interval)


def add_trading_mode_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--trading-mode",
        choices=TRADING_MODES,
        default=None,
        help="Explicit trading mode for private contract endpoints",
    )


def add_confirm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--confirm-live", action="store_true", help="Allow live mutating requests")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="WEEX Contract REST API helper",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "Optional contract API base URL override; leave empty to use "
            "WEEX_CONTRACT_API_BASE/WEEX_API_BASE or the built-in official default"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="HTTP timeout in seconds; leave empty to use WEEX_API_TIMEOUT or the built-in default",
    )
    groups = sorted({endpoint.group for endpoint in ENDPOINTS.values() if endpoint.group})

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser(
        "list-endpoints",
        help="List all supported contract REST endpoints",
        description="List the contract endpoint definitions bundled with this skill.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_list.add_argument("--group", choices=groups, default=None, help="Filter endpoints by contract endpoint group")
    p_list.add_argument("--pretty", action="store_true", help="Pretty-print JSON output for easier reading")

    p_call = sub.add_parser(
        "call",
        help="Call an endpoint by key with JSON query/body",
        description="Call a specific contract REST endpoint using raw JSON query and body payloads.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_call.add_argument("--endpoint", required=True, choices=sorted(ENDPOINTS.keys()), help="Exact endpoint key from list-endpoints")
    p_call.add_argument("--query", default="{}", help="JSON object string")
    p_call.add_argument("--body", default="{}", help="JSON object string")
    p_call.add_argument("--dry-run", action="store_true", help="Preview signed request without sending")
    add_trading_mode_argument(p_call)
    add_confirm_arguments(p_call)
    p_call.add_argument("--pretty", action="store_true", help="Pretty-print JSON output for easier reading")

    p_place = sub.add_parser(
        "place-order",
        help="Convenience wrapper for the live contract PlaceOrder doc",
        description="Place one contract order using the documented V3 fields exposed by this wrapper.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_place.add_argument("--symbol", required=True, help="Trading pair symbol, for example BTCUSDT or ETHUSDT")
    p_place.add_argument("--side", required=True, choices=["BUY", "SELL", "buy", "sell"], help="Order side: BUY opens/adds long exposure, SELL opens/adds short exposure depending on position side")
    p_place.add_argument("--position-side", required=True, choices=["LONG", "SHORT", "long", "short"], help="Position direction for the contract order")
    p_place.add_argument("--type", dest="order_type", required=True, choices=["LIMIT", "MARKET", "limit", "market"], help="Order type: LIMIT requires a price, MARKET sends immediately at market price")
    p_place.add_argument("--quantity", required=True, help="Order quantity as expected by WEEX for this contract")
    p_place.add_argument("--price", default=None, help="Limit price; usually required for LIMIT orders and omitted for MARKET orders")
    p_place.add_argument("--time-in-force", default=None, choices=["GTC", "IOC", "FOK", "POST_ONLY", "gtc", "ioc", "fok", "post_only"], help="Execution policy for LIMIT orders: GTC, IOC, FOK, or POST_ONLY")
    p_place.add_argument("--new-client-order-id", default=None, help="Optional client-defined order identifier; auto-generated when omitted")
    p_place.add_argument("--tp-trigger-price", default=None, help="Optional take-profit trigger price")
    p_place.add_argument("--sl-trigger-price", default=None, help="Optional stop-loss trigger price")
    p_place.add_argument("--tp-working-type", default=None, choices=["CONTRACT_PRICE", "MARK_PRICE", "contract_price", "mark_price"], help="Price source used to evaluate the take-profit trigger")
    p_place.add_argument("--sl-working-type", default=None, choices=["CONTRACT_PRICE", "MARK_PRICE", "contract_price", "mark_price"], help="Price source used to evaluate the stop-loss trigger")
    p_place.add_argument("--dry-run", action="store_true", help="Build and sign the request without sending it")
    add_trading_mode_argument(p_place)
    add_confirm_arguments(p_place)
    p_place.add_argument("--pretty", action="store_true", help="Pretty-print JSON output for easier reading")

    p_cancel = sub.add_parser(
        "cancel-order",
        help="Convenience wrapper for the live contract CancelOrder doc",
        description="Cancel one contract order by WEEX order id or client order id.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_cancel.add_argument("--order-id", default=None, help="WEEX order id to cancel")
    p_cancel.add_argument("--client-oid", default=None, help="Client order id to cancel when you do not have the WEEX order id")
    p_cancel.add_argument("--dry-run", action="store_true", help="Build and sign the cancel request without sending it")
    p_cancel.add_argument("--confirm-live", action="store_true", help="Allow the live cancel request")
    p_cancel.add_argument("--pretty", action="store_true", help="Pretty-print JSON output for easier reading")

    p_ticker = sub.add_parser(
        "ticker",
        help="Get ticker for one symbol",
        description="Fetch the current contract ticker for a single symbol.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_ticker.add_argument("--symbol", required=True, help="Trading pair symbol, for example BTCUSDT")
    p_ticker.add_argument("--pretty", action="store_true", help="Pretty-print JSON output for easier reading")

    p_poll = sub.add_parser(
        "poll-ticker",
        help="Continuously poll ticker",
        description="Repeatedly fetch the contract ticker for one symbol at a fixed interval.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p_poll.add_argument("--symbol", required=True, help="Trading pair symbol, for example BTCUSDT")
    p_poll.add_argument("--interval", type=float, default=2.0, help="Seconds to wait between requests")
    p_poll.add_argument("--count", type=int, default=0, help="0 means infinite")
    p_poll.add_argument("--pretty", action="store_true", help="Pretty-print JSON output for easier reading")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command_name = f"contract.{args.command}"
    try:
        refresh_agent_records(command=command_name)
    except Exception:
        pass

    requires_auth = command_requires_auth(args)
    environment_account = None
    if requires_auth:
        environment_account = load_environment_account()
        environment_validation = validate_runtime_environment()
        if not environment_validation["ok"]:
            raise SystemExit(
                "Invalid runtime environment:\n"
                + "\n".join(f"- {issue}" for issue in environment_validation["issues"])
            )
        try:
            ensure_private_runtime_ready(command=command_name, auto_setup=True)
        except RuntimePreflightError as exc:
            raise SystemExit(str(exc)) from exc

    env_base_url = os.getenv("WEEX_CONTRACT_API_BASE") or os.getenv("WEEX_API_BASE")
    base_url = (
        args.base_url
        or env_base_url
        or DEFAULT_BASE_URL
    )
    locale = os.getenv("WEEX_LOCALE") or DEFAULT_LOCALE
    timeout = args.timeout if args.timeout is not None else float(os.getenv("WEEX_API_TIMEOUT", DEFAULT_TIMEOUT))

    client = WeexContractClient(
        base_url=base_url,
        timeout=timeout,
        locale=locale,
        api_key=environment_account.credentials.api_key if environment_account else None,
        api_secret=environment_account.credentials.api_secret if environment_account else None,
        api_passphrase=environment_account.credentials.api_passphrase if environment_account else None,
    )

    if args.command == "list-endpoints":
        return cmd_list_endpoints(args)
    if args.command == "call":
        return cmd_call(args, client)
    if args.command == "place-order":
        return cmd_place_order(args, client)
    if args.command == "cancel-order":
        return cmd_cancel_order(args, client)
    if args.command == "ticker":
        return cmd_ticker(args, client)
    if args.command == "poll-ticker":
        return cmd_poll_ticker(args, client)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
