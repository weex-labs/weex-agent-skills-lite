#!/usr/bin/env python3
"""Collect and normalize official WEEX facts for guarded trading."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any



DAY_MS = 24 * 60 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
RECENT_ORDER_LOOKBACK_MS = HOUR_MS
RECENT_ORDER_HISTORY_UNAVAILABLE = "recent_order_history_unavailable"
MAX_FUTURES_WINDOW_DAYS = 90
POSITION_EPSILON = 0.00000001
FUTURES_ORDER_LIMIT = 1000
FUTURES_OPEN_ORDER_LIMIT = 100
FUTURES_PENDING_LIMIT = 100
SPOT_ORDER_LIMIT = 200
SPOT_ORDER_SAFE_LIMIT = 100
MAX_SPOT_HISTORY_WINDOW_DAYS = 90
KLINE_LIMIT = 100
DEFAULT_TRADING_MODE = "live"
TRADING_MODES = ("live",)
LONG_SIDES = {"long", "buy", "bull"}
SHORT_SIDES = {"short", "sell", "bear"}
SPOT_QUOTE_ASSET_FALLBACKS = ("USDT", "USDC", "BTC", "ETH")
SPOT_CASH_ASSETS = ("USDT", "USDC")


class AggregationInputError(ValueError):
    """Raised when the requested aggregation shape is not supported."""


@dataclass(frozen=True)
class TimeWindow:
    start_ms: int
    end_ms: int


def split_time_range(start_ms: int, end_ms: int, *, max_span_days: int) -> list[TimeWindow]:
    if start_ms < 0:
        raise AggregationInputError("start_ms must be non-negative.")
    if end_ms < start_ms:
        raise AggregationInputError("end_ms must be greater than or equal to start_ms.")
    if max_span_days <= 0:
        raise AggregationInputError("max_span_days must be positive.")

    max_span_ms = max_span_days * DAY_MS
    windows: list[TimeWindow] = []
    cursor = start_ms
    while cursor <= end_ms:
        next_end = min(end_ms, cursor + max_span_ms - 1)
        windows.append(TimeWindow(start_ms=cursor, end_ms=next_end))
        cursor = next_end + 1
    return windows


def _validate_market(market: str) -> str:
    normalized = str(market).strip().lower()
    if normalized not in {"futures", "spot", "all"}:
        raise AggregationInputError("market must be one of futures, spot, or all.")
    return normalized


def _normalize_trading_mode(raw: Any) -> str:
    mode = str(raw or DEFAULT_TRADING_MODE).strip().lower()
    if mode == "demo":
        raise AggregationInputError("DEMO_MODE_REMOVED: demo trading is no longer supported")
    if mode not in TRADING_MODES:
        raise AggregationInputError(f"invalid_trading_mode: expected one of {', '.join(TRADING_MODES)}")
    return mode


def _validate_trading_mode_market(trading_mode: str, market: str) -> str:
    return _normalize_trading_mode(trading_mode)


def _environment_for_trading_mode(trading_mode: str, market: str) -> dict[str, Any]:
    _normalize_trading_mode(trading_mode)
    environment = {
        "trading_mode": "live",
        "label": "live",
        "market": market,
        "uses_real_funds": True,
    }
    return environment


def _normalize_symbol_for_trading_mode(raw: Any, trading_mode: str) -> str:
    _normalize_trading_mode(trading_mode)
    return str(raw or "UNKNOWN")


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _pick(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _coerce_bool(value: Any) -> bool | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _normalize_account_scope(
    market: str,
    mapping: dict[str, Any] | None = None,
    *,
    trading_mode: str = DEFAULT_TRADING_MODE,
) -> str:
    explicit = _pick(mapping or {}, "account_scope", "accountScope")
    if explicit not in (None, ""):
        return str(explicit)
    if market == "futures":
        return "personal_futures"
    if market == "spot":
        return "personal_spot"
    return f"personal_{market}"


def _normalize_margin_type(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _normalize_position_mode(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip().upper()
    if normalized in {"ONE_WAY", "ONEWAY"}:
        return "COMBINED"
    if normalized == "HEDGE":
        return "SEPARATED"
    return normalized or None


def _extract_list_payload(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extend_unique_dict_rows(target: list[dict[str, Any]], rows: list[dict[str, Any]], *, identity_keys: tuple[str, ...]) -> None:
    seen = {
        tuple(str(item.get(key) or "") for key in identity_keys)
        for item in target
    }
    for row in rows:
        identity = tuple(str(row.get(key) or "") for key in identity_keys)
        if identity in seen:
            continue
        seen.add(identity)
        target.append(row)


def _merge_degraded_reasons(target: list[str], reasons: list[str]) -> None:
    for reason in reasons:
        if reason and reason not in target:
            target.append(reason)


def _merge_constraints(target: list[dict[str, Any]], constraints: list[dict[str, Any]]) -> None:
    seen = {
        (str(item.get("code") or ""), str(item.get("message") or ""))
        for item in target
    }
    for item in constraints:
        code = str(item.get("code") or "")
        message = str(item.get("message") or "")
        identity = (code, message)
        if identity in seen:
            continue
        seen.add(identity)
        target.append({"code": code, "message": message})


def _should_retry_spot_history_orders_with_safe_limit(error: Exception, *, limit: int) -> bool:
    if limit <= SPOT_ORDER_SAFE_LIMIT:
        return False
    message = str(error).lower()
    return (
        "spot.order.history_orders" in message
        and (
            "unknown error occurred" in message
            or "'code': -1000" in message
            or '"code": -1000' in message
            or "'code': -1142" in message
            or '"code": -1142' in message
            or "between 1 and 200" in message
        )
    )


def _should_degrade_spot_balance_error(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "spot.account.get_account_balance" in message
        and ("unknown error occurred" in message or "'code': -1000" in message or '"code": -1000' in message)
    )


def _should_degrade_spot_kline_error(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "spot.market.get_k_line_data" in message
        and (
            "rate limit exceeded" in message
            or "'code': 429" in message
            or '"code": 429' in message
            or "unknown error occurred" in message
            or "'code': -1000" in message
            or '"code": -1000' in message
        )
    )


def _normalize_trade_position_side(raw_position_side: Any, *, market: str, fallback_side: Any = None) -> str | None:
    side_text = str(raw_position_side or "").strip().lower()
    if side_text in LONG_SIDES:
        return "long"
    if side_text in SHORT_SIDES:
        return "short"
    if market == "spot":
        return "long"

    fallback_text = str(fallback_side or "").strip().lower()
    if fallback_text in LONG_SIDES:
        return "long"
    if fallback_text in SHORT_SIDES:
        return "short"
    return None


def _infer_fill_action(fill: dict[str, Any], order: dict[str, Any], *, market: str) -> str:
    if order.get("reduce_only") or order.get("close_position"):
        return "exit"

    side = str(fill.get("side") or order.get("side") or "").strip().lower()
    position_side = _normalize_trade_position_side(
        fill.get("position_side") or order.get("position_side"),
        market=market,
        fallback_side=side,
    )
    if market == "spot":
        return "entry" if side in LONG_SIDES else "exit"
    if position_side == "long":
        return "entry" if side in LONG_SIDES else "exit"
    if position_side == "short":
        return "entry" if side in SHORT_SIDES else "exit"
    return "entry" if side in LONG_SIDES else "exit"


def _remaining_order_quantity(entry: dict[str, Any]) -> float | None:
    quantity = _to_float(entry.get("quantity"))
    if quantity is None:
        return None
    executed_qty = _to_float(entry.get("executed_qty")) or 0.0
    return max(0.0, abs(quantity) - abs(executed_qty))


def _position_bucket(entry: dict[str, Any], *, market: str) -> tuple[str, str]:
    symbol = str(entry.get("symbol") or "UNKNOWN").strip().upper()
    position_side = _normalize_trade_position_side(
        entry.get("position_side"),
        market=market,
        fallback_side=entry.get("side"),
    ) or "net"
    return symbol, position_side


def _normalize_order_identifier(row: dict[str, Any]) -> str:
    for key in ("order_id", "orderId", "actualOrderId", "algoId"):
        value = _pick(row, key)
        if value in (None, "", 0, "0"):
            continue
        return str(value)
    return ""


def _matching_bucket_quantity(
    rows: list[dict[str, Any]],
    *,
    market: str,
    symbol: str,
    position_side: str,
) -> float:
    total = 0.0
    target = (symbol, position_side)
    for row in rows:
        if _position_bucket(row, market=market) != target:
            continue
        quantity = abs(_to_float(row.get("quantity")) or 0.0)
        total += quantity
    return total


def _matching_working_order_quantity(
    rows: list[dict[str, Any]],
    *,
    market: str,
    symbol: str,
    position_side: str,
    action: str,
) -> float:
    total = 0.0
    target = (symbol, position_side)
    for row in rows:
        if _position_bucket(row, market=market) != target:
            continue
        if _infer_fill_action(row, row, market=market) != action:
            continue
        quantity = _remaining_order_quantity(row)
        if quantity is None:
            continue
        total += quantity
    return total


def _build_order_risk_tp_sl_state(
    *,
    market: str,
    symbol: str | None,
    position_side: str | None,
    preview_quantity: float | None,
    preview_has_take_profit: bool,
    preview_has_stop_loss: bool,
    positions: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    conditional_orders: list[dict[str, Any]],
) -> dict[str, float | bool]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_position_side = _normalize_trade_position_side(
        position_side,
        market=market,
        fallback_side=None,
    ) or "net"
    required_qty = _matching_bucket_quantity(
        positions,
        market=market,
        symbol=normalized_symbol,
        position_side=normalized_position_side,
    )
    required_qty += _matching_working_order_quantity(
        open_orders,
        market=market,
        symbol=normalized_symbol,
        position_side=normalized_position_side,
        action="entry",
    )
    required_qty += abs(preview_quantity or 0.0)

    reserved_close_qty = _matching_working_order_quantity(
        open_orders,
        market=market,
        symbol=normalized_symbol,
        position_side=normalized_position_side,
        action="exit",
    )

    take_profit_covered_qty = abs(preview_quantity or 0.0) if preview_has_take_profit else 0.0
    stop_loss_covered_qty = abs(preview_quantity or 0.0) if preview_has_stop_loss else 0.0

    for order in conditional_orders:
        if _position_bucket(order, market=market) != (normalized_symbol, normalized_position_side):
            continue
        remaining_qty = _remaining_order_quantity(order)
        if order.get("close_position"):
            remaining_qty = required_qty
        if remaining_qty is None:
            continue
        if order.get("tp_trigger_price") not in (None, ""):
            take_profit_covered_qty += remaining_qty
        if order.get("sl_trigger_price") not in (None, ""):
            stop_loss_covered_qty += remaining_qty

    take_profit_covered_qty = max(0.0, take_profit_covered_qty - reserved_close_qty)
    stop_loss_covered_qty = max(0.0, stop_loss_covered_qty - reserved_close_qty)
    has_take_profit = required_qty > POSITION_EPSILON and take_profit_covered_qty + POSITION_EPSILON >= required_qty
    has_stop_loss = required_qty > POSITION_EPSILON and stop_loss_covered_qty + POSITION_EPSILON >= required_qty

    return {
        "has_take_profit": has_take_profit,
        "has_stop_loss": has_stop_loss,
        "required_covered_qty": round(required_qty, 8),
        "take_profit_covered_qty": round(take_profit_covered_qty, 8),
        "stop_loss_covered_qty": round(stop_loss_covered_qty, 8),
    }


def _extract_rows(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
    return []


def _ensure_dict_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return [payload]
    return _extract_rows(payload)


def _normalize_balance_entries(
    payload: Any,
    market: str,
    *,
    trading_mode: str = DEFAULT_TRADING_MODE,
) -> list[dict[str, Any]]:
    if market == "spot":
        rows = _extract_rows(payload, "balances")
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            available_balance = _to_float(_pick(row, "availableBalance", "free"))
            locked = _to_float(_pick(row, "locked"))
            balance = _to_float(_pick(row, "balance"))
            if balance is None and (available_balance is not None or locked is not None):
                balance = (available_balance or 0.0) + (locked or 0.0)
            normalized_rows.append(
                {
                    "account_scope": _normalize_account_scope(market, row, trading_mode=trading_mode),
                    "market": market,
                    "asset": str(_pick(row, "asset", "coinName") or "UNKNOWN"),
                    "balance": balance,
                    "available_balance": available_balance,
                    "locked": locked,
                    "equity": None,
                    "unrealized_pnl": None,
                }
            )
        return normalized_rows

    rows = _ensure_dict_rows(payload)
    return [
        {
            "account_scope": _normalize_account_scope(market, row, trading_mode=trading_mode),
            "market": market,
            "asset": str(_pick(row, "asset") or "UNKNOWN"),
            "balance": _to_float(_pick(row, "balance")),
            "available_balance": _to_float(_pick(row, "availableBalance")),
            "locked": _to_float(_pick(row, "frozen")),
            "equity": _to_float(_pick(row, "balance")),
            "unrealized_pnl": _to_float(_pick(row, "unrealizePnl", "unrealizedPnl")),
        }
        for row in rows
    ]


def _normalize_positions(
    payload: Any,
    market: str,
    *,
    trading_mode: str = DEFAULT_TRADING_MODE,
) -> list[dict[str, Any]]:
    rows = _extract_rows(payload, "positions", "items")
    if not rows and isinstance(payload, list):
        rows = [item for item in payload if isinstance(item, dict)]
    normalized_rows: list[dict[str, Any]] = []
    for row in rows:
        quantity = _to_float(_pick(row, "size", "quantity", "qty"))
        side = str(_pick(row, "side", "positionSide") or "unknown").lower()
        position_id = _pick(row, "positionId", "position_id", "id")
        separated_open_order_id = _pick(
            row,
            "separatedOpenOrderId",
            "separated_open_order_id",
        )
        open_value = _to_float(_pick(row, "openValue", "open_value"))
        unrealized_pnl = _to_float(
            _pick(row, "unrealizePnl", "unrealizedPnl", "unrealized_pnl")
        )
        mark_price = _to_float(
            _pick(
                row,
                "markPrice",
                "mark_price",
                "currentPrice",
                "current_price",
                "lastPrice",
                "last_price",
            )
        )
        notional = _to_float(
            _pick(
                row,
                "notional",
                "positionValue",
                "position_value",
                "markValue",
                "mark_value",
                "currentValue",
                "current_value",
                "value",
            )
        )
        absolute_quantity = abs(quantity) if quantity is not None else None
        entry_price = None
        if (
            open_value is not None
            and absolute_quantity is not None
            and absolute_quantity > POSITION_EPSILON
        ):
            entry_price = abs(open_value) / absolute_quantity
        if notional is not None:
            notional = abs(notional)
        elif mark_price is not None and absolute_quantity is not None:
            notional = absolute_quantity * abs(mark_price)
        elif open_value is not None and unrealized_pnl is not None:
            if side in {"long", "buy"}:
                candidate_notional = abs(open_value) + unrealized_pnl
            elif side in {"short", "sell"}:
                candidate_notional = abs(open_value) - unrealized_pnl
            else:
                candidate_notional = None
            if candidate_notional is not None and candidate_notional >= 0.0:
                notional = candidate_notional
        if mark_price is not None:
            mark_price = abs(mark_price)
        elif (
            notional is not None
            and absolute_quantity is not None
            and absolute_quantity > POSITION_EPSILON
        ):
            mark_price = notional / absolute_quantity

        normalized_rows.append(
            {
                "account_scope": _normalize_account_scope(
                    market,
                    row,
                    trading_mode=trading_mode,
                ),
                "market": market,
                "symbol": _normalize_symbol_for_trading_mode(
                    _pick(row, "symbol", "instId"),
                    trading_mode,
                ),
                "position_id": None if position_id in (None, "") else str(position_id),
                "separated_open_order_id": (
                    None
                    if separated_open_order_id in (None, "")
                    else str(separated_open_order_id)
                ),
                "side": side,
                "margin_type": _normalize_margin_type(_pick(row, "marginType", "margin_type")),
                "position_mode": _normalize_position_mode(
                    _pick(row, "positionMode", "position_mode", "separatedMode")
                ),
                "quantity": quantity,
                "open_value": open_value,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "notional": notional,
                "unrealized_pnl": unrealized_pnl,
                "margin_size": _to_float(_pick(row, "marginSize", "margin_size")),
                "liquidation_price": _to_float(
                    _pick(
                        row,
                        "liquidatePrice",
                        "liquidationPrice",
                        "liquidation_price",
                        "liqPrice",
                    )
                ),
                "leverage": _to_float(_pick(row, "leverage")),
                "created_time": int(_pick(row, "createdTime", "time") or 0),
                "updated_time": int(_pick(row, "updatedTime", "updateTime") or 0),
            }
        )
    return normalized_rows


def _normalize_orders(
    payload: Any,
    market: str,
    *,
    trading_mode: str = DEFAULT_TRADING_MODE,
) -> list[dict[str, Any]]:
    rows = _extract_rows(payload, "orders", "items")
    if not rows and isinstance(payload, list):
        rows = [item for item in payload if isinstance(item, dict)]
    return [
        {
            "account_scope": _normalize_account_scope(market, row, trading_mode=trading_mode),
            "market": market,
            "symbol": _normalize_symbol_for_trading_mode(_pick(row, "symbol"), trading_mode),
            "order_id": _normalize_order_identifier(row),
            "algo_id": str(_pick(row, "algoId") or ""),
            "client_order_id": str(
                _pick(row, "client_order_id", "clientOrderId", "clientAlgoId", "origClientOrderId") or ""
            ),
            "side": str(_pick(row, "side") or "unknown").lower(),
            "position_side": str(_pick(row, "position_side", "positionSide") or "").lower() or None,
            "margin_type": _normalize_margin_type(_pick(row, "marginType", "margin_type")),
            "position_mode": _normalize_position_mode(
                _pick(row, "positionMode", "position_mode", "separatedMode")
            ),
            "order_type": str(_pick(row, "type", "orderType") or "unknown").lower(),
            "status": str(_pick(row, "status", "algoStatus") or "unknown"),
            "reduce_only": bool(_coerce_bool(_pick(row, "reduceOnly", "reduce_only"))),
            "close_position": bool(_coerce_bool(_pick(row, "closePosition", "close_position"))),
            "working_type": str(_pick(row, "workingType", "working_type") or ""),
            "quantity": _to_float(_pick(row, "origQty", "quantity")),
            "executed_qty": _to_float(_pick(row, "executedQty")),
            "quote_qty": _to_float(_pick(row, "cumQuote", "cummulativeQuoteQty")),
            "avg_price": _to_float(_pick(row, "avgPrice")),
            "price": _to_float(_pick(row, "price")),
            "tp_trigger_price": _to_float(_pick(row, "tpTriggerPrice", "tp_trigger_price")),
            "tp_price": _to_float(_pick(row, "tpPrice", "tp_price")),
            "sl_trigger_price": _to_float(_pick(row, "slTriggerPrice", "sl_trigger_price")),
            "sl_price": _to_float(_pick(row, "slPrice", "sl_price")),
            "time": int(_pick(row, "time", "createdTime", "createTime") or 0),
            "update_time": int(_pick(row, "updateTime", "updatedTime") or 0),
            "trigger_time": int(_pick(row, "triggerTime") or 0),
        }
        for row in rows
    ]


def _normalize_klines(payload: Any, market: str, symbol: str | None) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, list) or len(item) < 7:
            continue
        rows.append(
            {
                "market": market,
                "symbol": symbol or "UNKNOWN",
                "open_time": int(item[0]),
                "open": _to_float(item[1]),
                "high": _to_float(item[2]),
                "low": _to_float(item[3]),
                "close": _to_float(item[4]),
                "volume": _to_float(item[5]),
                "close_time": int(item[6]),
                "quote_volume": _to_float(item[7]) if len(item) > 7 else None,
                "trades": int(item[8]) if len(item) > 8 and str(item[8]).isdigit() else None,
            }
        )
    return rows


def _extract_symbol_product_info(payload: Any, symbol: str) -> dict[str, Any] | None:
    raw = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    rows: list[Any]
    if isinstance(raw, dict) and isinstance(raw.get("symbols"), list):
        rows = raw["symbols"]
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    normalized = str(symbol or "").strip().upper()
    matches = [
        row for row in rows
        if isinstance(row, dict) and str(row.get("symbol") or row.get("instId") or "").strip().upper() == normalized
    ]
    return dict(matches[0]) if len(matches) == 1 else None


def _safe_current_price_from_klines(
    *,
    fetch_klines: Any,
    market: str,
    symbol: str,
    degraded_reasons: list[str],
) -> float | None:
    try:
        candles = _normalize_klines(fetch_klines(), market, symbol)
    except AggregationInputError:
        _merge_degraded_reasons(degraded_reasons, [f"{market}_market_snapshot_unavailable"])
        return None
    if candles:
        return candles[-1]["close"]
    return None


def _extract_latest_price(payload: Any) -> float | None:
    candidates: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        candidates.append(payload)
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        elif isinstance(data, list):
            candidates.extend(item for item in data if isinstance(item, dict))
    elif isinstance(payload, list):
        candidates.extend(item for item in payload if isinstance(item, dict))

    for candidate in candidates:
        price = _to_float(_pick(candidate, "lastPrice", "price", "markPrice", "close"))
        if price is not None and math.isfinite(price) and price > 0:
            return price
    return None


def _safe_current_price(
    *,
    fetch_latest_price: Any,
    market: str,
    symbol: str,
    degraded_reasons: list[str],
    fetch_klines: Any | None = None,
) -> float | None:
    try:
        latest_price = _extract_latest_price(fetch_latest_price())
    except AggregationInputError:
        latest_price = None
    if latest_price is not None:
        return latest_price
    if fetch_klines is not None:
        return _safe_current_price_from_klines(
            fetch_klines=fetch_klines,
            market=market,
            symbol=symbol,
            degraded_reasons=degraded_reasons,
        )
    _merge_degraded_reasons(degraded_reasons, [f"{market}_market_snapshot_unavailable"])
    return None


def _infer_spot_quote_asset(symbol: str | None, balances: list[dict[str, Any]]) -> str | None:
    if not symbol:
        return None
    normalized_symbol = str(symbol).strip().upper()
    candidate_assets = {
        str(row.get("asset") or "").strip().upper()
        for row in balances
        if str(row.get("asset") or "").strip()
    }
    candidate_assets.update(SPOT_QUOTE_ASSET_FALLBACKS)
    for asset in sorted(candidate_assets, key=len, reverse=True):
        if asset and normalized_symbol.endswith(asset) and len(normalized_symbol) > len(asset):
            return asset
    return None


def _extract_spot_balance_value_usdt(
    *,
    asset: str,
    amount: float | None,
    fetch_price: Any,
    degraded_reasons: list[str],
) -> float | None:
    if amount is None:
        return None
    if amount == 0.0:
        return 0.0

    normalized_asset = str(asset).strip().upper()
    if normalized_asset == "USDT":
        return amount

    try:
        price_payload = fetch_price(symbol=f"{normalized_asset}USDT")
    except AggregationInputError:
        price_payload = None

    price = _extract_latest_price(price_payload)
    if price is None:
        _merge_degraded_reasons(degraded_reasons, ["spot_equity_estimate_partial"])
        return None
    return amount * price


def _build_spot_account_estimates(
    *,
    balances: list[dict[str, Any]],
    symbol: str | None,
    fetch_spot_latest_price: Any,
    degraded_reasons: list[str],
) -> tuple[dict[str, float | None], list[dict[str, Any]]]:
    equity_total = 0.0
    saw_equity_component = False
    available_equity_total = 0.0
    saw_available_component = False
    positions: list[dict[str, Any]] = []
    price_cache: dict[str, Any] = {}

    def cached_fetch_spot_latest_price(*, symbol: str) -> Any:
        if symbol not in price_cache:
            price_cache[symbol] = fetch_spot_latest_price(symbol=symbol)
        return price_cache[symbol]

    for row in balances:
        asset = str(row.get("asset") or "").strip().upper()
        if not asset:
            continue

        balance_amount = _to_float(row.get("balance"))
        available_amount = _to_float(row.get("available_balance"))
        balance_value = _extract_spot_balance_value_usdt(
            asset=asset,
            amount=balance_amount,
            fetch_price=cached_fetch_spot_latest_price,
            degraded_reasons=degraded_reasons,
        )
        if balance_value is not None:
            equity_total += balance_value
            saw_equity_component = True

        available_value = _extract_spot_balance_value_usdt(
            asset=asset,
            amount=available_amount,
            fetch_price=cached_fetch_spot_latest_price,
            degraded_reasons=degraded_reasons,
        )
        if available_value is not None:
            available_equity_total += available_value
            saw_available_component = True

        if asset in SPOT_CASH_ASSETS:
            continue
        if balance_amount is None or abs(balance_amount) <= POSITION_EPSILON:
            continue
        if balance_value is None or abs(balance_value) <= POSITION_EPSILON:
            continue

        positions.append(
            {
                "account_scope": str(row.get("account_scope") or _normalize_account_scope("spot", row)),
                "market": "spot",
                "symbol": f"{asset}USDT",
                "side": "long",
                "margin_type": None,
                "position_mode": "COMBINED",
                "quantity": balance_amount,
                "notional": balance_value,
                "leverage": 1.0,
                "created_time": 0,
                "updated_time": 0,
            }
        )

    quote_asset = _infer_spot_quote_asset(symbol, balances)
    base_asset = None
    base_available_quantity = None
    quote_available_balance = None
    quote_available_balance_u = None
    if quote_asset:
        normalized_symbol = str(symbol or "").strip().upper()
        base_asset = normalized_symbol[: -len(quote_asset)] or None
        base_row = next(
            (
                row
                for row in balances
                if str(row.get("asset") or "").strip().upper() == base_asset
            ),
            None,
        )
        if base_row is not None:
            base_available_quantity = _to_float(base_row.get("available_balance"))
        quote_row = next(
            (
                row
                for row in balances
                if str(row.get("asset") or "").strip().upper() == quote_asset
            ),
            None,
        )
        if quote_row is not None:
            quote_available_balance = _to_float(quote_row.get("available_balance"))
            quote_available_balance_u = _extract_spot_balance_value_usdt(
                asset=quote_asset,
                amount=quote_available_balance,
                fetch_price=cached_fetch_spot_latest_price,
                degraded_reasons=degraded_reasons,
            )

    positions.sort(key=lambda row: abs(_to_float(row.get("notional")) or 0.0), reverse=True)
    return (
        {
            "equity": equity_total if saw_equity_component else None,
            "available_balance": (
                quote_available_balance_u
                if quote_asset is not None
                else (available_equity_total if saw_available_component else None)
            ),
            "base_asset": base_asset,
            "base_available_quantity": base_available_quantity,
            "quote_asset": quote_asset,
            "quote_available_balance": quote_available_balance,
            "quote_available_balance_u": quote_available_balance_u,
        },
        positions,
    )


def _estimate_spot_account_snapshot(
    *,
    balances: list[dict[str, Any]],
    symbol: str | None,
    fetch_spot_latest_price: Any,
    degraded_reasons: list[str],
) -> dict[str, float | None]:
    account_snapshot, _ = _build_spot_account_estimates(
        balances=balances,
        symbol=symbol,
        fetch_spot_latest_price=fetch_spot_latest_price,
        degraded_reasons=degraded_reasons,
    )
    return account_snapshot


def _estimate_futures_position_price(
    *,
    positions: list[dict[str, Any]],
    symbol: str | None,
) -> float | None:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return None

    for row in positions:
        if str(row.get("symbol") or "").strip().upper() != normalized_symbol:
            continue
        mark_price = abs(_to_float(row.get("mark_price")) or 0.0)
        if mark_price > 0.0:
            return mark_price
        quantity = abs(_to_float(row.get("quantity")) or 0.0)
        notional = abs(_to_float(row.get("notional")) or 0.0)
        if quantity <= POSITION_EPSILON or notional <= 0.0:
            continue
        return notional / quantity
    return None


def _apply_futures_position_price(
    *,
    positions: list[dict[str, Any]],
    symbol: str | None,
    current_price: float | None,
) -> None:
    normalized_symbol = str(symbol or "").strip().upper()
    price = abs(_to_float(current_price) or 0.0)
    if not normalized_symbol or price <= 0.0:
        return
    for row in positions:
        if str(row.get("symbol") or "").strip().upper() != normalized_symbol:
            continue
        quantity = abs(_to_float(row.get("quantity")) or 0.0)
        if quantity <= POSITION_EPSILON:
            continue
        row["mark_price"] = price
        row["notional"] = quantity * price


def _mark_market_snapshot_estimated(
    *,
    degraded_reasons: list[str],
    market: str,
    reason: str,
) -> None:
    unavailable_reason = f"{market}_market_snapshot_unavailable"
    while unavailable_reason in degraded_reasons:
        degraded_reasons.remove(unavailable_reason)
    _merge_degraded_reasons(degraded_reasons, [reason])


def _pick_primary_futures_symbol(
    *,
    positions: list[dict[str, Any]],
    recent_orders: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    conditional_orders: list[dict[str, Any]],
) -> str | None:
    if positions:
        primary_position = max(
            positions,
            key=lambda row: abs(
                _to_float(row.get("notional"))
                or _to_float(row.get("open_value"))
                or 0.0
            ),
        )
        symbol = str(primary_position.get("symbol") or "").strip().upper()
        if symbol:
            return symbol

    for collection in (recent_orders, open_orders, conditional_orders):
        ranked = sorted(
            collection,
            key=lambda row: int(_pick(row, "time", "update_time", "updateTime", "created_time", "createdTime") or 0),
            reverse=True,
        )
        for row in ranked:
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                return symbol
    return None


class TradeDataAggregator:
    def __init__(self, fetcher: Any | None = None) -> None:
        self.fetcher = fetcher or WeexApiFetcher()

    def _collect_spot_balances(
        self,
        *,
        profile_name: str,
        degraded_reasons: list[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            payload = self.fetcher.fetch_spot_balance(profile_name=profile_name)
        except AggregationInputError as exc:
            if _should_degrade_spot_balance_error(exc):
                _merge_degraded_reasons(degraded_reasons, ["spot_balance_unavailable"])
                return [], True
            raise
        balances = _normalize_balance_entries(payload, "spot")
        if not balances:
            _merge_degraded_reasons(degraded_reasons, ["spot_balance_unavailable"])
            return [], True
        return balances, False

    def _collect_recent_futures_orders(
        self,
        *,
        profile_name: str,
        trading_mode: str,
        start_ms: int,
        end_ms: int,
        symbol: str | None,
        degraded_reasons: list[str],
        constraints: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            payload = self.fetcher.fetch_futures_orders(
                profile_name=profile_name,
                trading_mode=trading_mode,
                start_ms=start_ms,
                end_ms=end_ms,
                symbol=symbol,
            )
        except Exception as exc:
            _merge_degraded_reasons(degraded_reasons, [RECENT_ORDER_HISTORY_UNAVAILABLE])
            _merge_constraints(
                constraints,
                [
                    {
                        "code": RECENT_ORDER_HISTORY_UNAVAILABLE,
                        "message": f"Recent order history could not be collected: {exc}",
                    }
                ],
            )
            return [], True
        return _normalize_orders(payload, "futures", trading_mode=trading_mode), False

    def _collect_recent_spot_orders(
        self,
        *,
        profile_name: str,
        start_ms: int,
        end_ms: int,
        symbol: str | None,
        degraded_reasons: list[str],
        constraints: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            payload = self.fetcher.fetch_spot_orders(
                profile_name=profile_name,
                start_ms=start_ms,
                end_ms=end_ms,
                symbol=symbol,
            )
        except Exception as exc:
            _merge_degraded_reasons(degraded_reasons, [RECENT_ORDER_HISTORY_UNAVAILABLE])
            _merge_constraints(
                constraints,
                [
                    {
                        "code": RECENT_ORDER_HISTORY_UNAVAILABLE,
                        "message": f"Recent spot order history could not be collected: {exc}",
                    }
                ],
            )
            return [], True
        return _normalize_orders(payload, "spot"), False

    def collect_order_risk_payload(
        self,
        *,
        profile_name: str,
        market: str,
        trading_mode: str = DEFAULT_TRADING_MODE,
        raw_order: dict[str, Any],
        language: str | None = None,
    ) -> dict[str, Any]:
        normalized_market = _validate_market(market)
        mode = _validate_trading_mode_market(trading_mode, normalized_market)
        environment = _environment_for_trading_mode(mode, normalized_market)
        if normalized_market == "all":
            raise AggregationInputError("order preview requires a concrete market, not 'all'.")

        symbol = str(_pick(raw_order, "symbol") or "").strip().upper() or None
        tp_trigger = _pick(raw_order, "tp_trigger_price", "tpTriggerPrice")
        sl_trigger = _pick(raw_order, "sl_trigger_price", "slTriggerPrice")
        order_preview = {
            "market": normalized_market,
            "symbol": symbol,
            "side": str(_pick(raw_order, "side") or "").upper(),
            "position_side": str(_pick(raw_order, "position_side", "positionSide") or "").upper() or None,
            "order_type": str(_pick(raw_order, "order_type", "type") or "").upper(),
            "quantity": _to_float(_pick(raw_order, "quantity")),
            "price": _to_float(_pick(raw_order, "price")),
            "time_in_force": _pick(raw_order, "time_in_force", "timeInForce"),
        }
        tp_sl = {
            "has_take_profit": tp_trigger not in (None, ""),
            "has_stop_loss": sl_trigger not in (None, ""),
        }
        partial = False
        degraded_reasons: list[str] = []
        constraints: list[dict[str, Any]] = []

        balances: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        recent_orders: list[dict[str, Any]] = []
        conditional_orders: list[dict[str, Any]] = []
        open_orders: list[dict[str, Any]] = []
        current_price: float | None = None
        product_facts: dict[str, Any] | None = None
        end_ms = int(time.time() * 1000)
        start_ms = max(0, end_ms - RECENT_ORDER_LOOKBACK_MS)

        if normalized_market == "futures":
            balances = _normalize_balance_entries(
                self.fetcher.fetch_futures_balance(profile_name=profile_name, trading_mode=mode),
                "futures",
                trading_mode=mode,
            )
            if not balances:
                partial = True
                _merge_degraded_reasons(degraded_reasons, ["futures_balance_unavailable"])
            positions = _normalize_positions(
                self.fetcher.fetch_futures_positions(profile_name=profile_name, trading_mode=mode),
                "futures",
                trading_mode=mode,
            )
            recent_orders, recent_orders_partial = self._collect_recent_futures_orders(
                profile_name=profile_name,
                trading_mode=mode,
                start_ms=start_ms,
                end_ms=end_ms,
                symbol=symbol,
                degraded_reasons=degraded_reasons,
                constraints=constraints,
            )
            partial = partial or recent_orders_partial
            open_orders = _normalize_orders(
                self.fetcher.fetch_futures_open_orders(
                    profile_name=profile_name,
                    symbol=symbol,
                ),
                "futures",
            )
            conditional_orders = _normalize_orders(
                self.fetcher.fetch_futures_pending_orders(
                    profile_name=profile_name,
                    symbol=symbol,
                ),
                "futures",
            )
            if symbol:
                fetch_product = getattr(self.fetcher, "fetch_futures_product_info", None)
                if callable(fetch_product):
                    try:
                        product_facts = _extract_symbol_product_info(fetch_product(symbol=symbol), symbol)
                    except Exception:
                        product_facts = None
                if product_facts is None:
                    partial = True
                    _merge_degraded_reasons(degraded_reasons, ["futures_product_rules_unavailable"])
                current_price = _safe_current_price(
                    fetch_latest_price=lambda: self.fetcher.fetch_futures_latest_price(symbol=symbol),
                    market="futures",
                    symbol=symbol,
                    degraded_reasons=degraded_reasons,
                    fetch_klines=lambda: self.fetcher.fetch_futures_klines(
                        symbol=symbol,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    ),
                )
                if current_price is None:
                    current_price = _estimate_futures_position_price(
                        positions=positions,
                        symbol=symbol,
                    )
                    if current_price is not None:
                        _mark_market_snapshot_estimated(
                            degraded_reasons=degraded_reasons,
                            market="futures",
                            reason="futures_market_snapshot_estimated_from_position",
                        )
                if current_price is None:
                    partial = True
                else:
                    _apply_futures_position_price(
                        positions=positions,
                        symbol=symbol,
                        current_price=current_price,
                    )
        else:
            balances, spot_balance_partial = self._collect_spot_balances(
                profile_name=profile_name,
                degraded_reasons=degraded_reasons,
            )
            partial = partial or spot_balance_partial
            recent_orders, recent_orders_partial = self._collect_recent_spot_orders(
                profile_name=profile_name,
                start_ms=start_ms,
                end_ms=end_ms,
                symbol=symbol,
                degraded_reasons=degraded_reasons,
                constraints=constraints,
            )
            partial = partial or recent_orders_partial
            if symbol:
                fetch_product = getattr(self.fetcher, "fetch_spot_product_info", None)
                if callable(fetch_product):
                    try:
                        product_facts = _extract_symbol_product_info(fetch_product(symbol=symbol), symbol)
                    except Exception:
                        product_facts = None
                if product_facts is None:
                    partial = True
                    _merge_degraded_reasons(degraded_reasons, ["spot_product_rules_unavailable"])
                current_price = _safe_current_price(
                    fetch_latest_price=lambda: self.fetcher.fetch_spot_latest_price(
                        profile_name=profile_name,
                        symbol=symbol,
                    ),
                    market="spot",
                    symbol=symbol,
                    degraded_reasons=degraded_reasons,
                    fetch_klines=lambda: self.fetcher.fetch_spot_klines(
                        profile_name=profile_name,
                        symbol=symbol,
                    ),
                )
            open_orders = _normalize_orders(
                self.fetcher.fetch_spot_open_orders(
                    profile_name=profile_name,
                    symbol=symbol,
                ),
                "spot",
            )
            _merge_degraded_reasons(degraded_reasons, ["spot_tp_sl_state_unavailable"])

        primary_balance = None
        if balances:
            primary_balance = next((row for row in balances if row.get("asset") == "USDT"), balances[0])
        if normalized_market == "spot":
            account_snapshot, positions = _build_spot_account_estimates(
                balances=balances,
                symbol=symbol,
                fetch_spot_latest_price=lambda *, symbol: self.fetcher.fetch_spot_latest_price(
                    profile_name=profile_name,
                    symbol=symbol,
                ),
                degraded_reasons=degraded_reasons,
            )
        else:
            account_snapshot = {
                "account_scope": "personal_futures",
                "equity": primary_balance.get("equity") if primary_balance else None,
                "available_balance": primary_balance.get("available_balance") if primary_balance else None,
            }

        if normalized_market == "futures":
            tp_sl.update(
                _build_order_risk_tp_sl_state(
                    market="futures",
                    symbol=symbol,
                    position_side=str(order_preview.get("position_side") or ""),
                    preview_quantity=_to_float(order_preview.get("quantity")),
                    preview_has_take_profit=bool(tp_sl.get("has_take_profit")),
                    preview_has_stop_loss=bool(tp_sl.get("has_stop_loss")),
                    positions=positions,
                    open_orders=open_orders,
                    conditional_orders=conditional_orders,
                )
            )

        return {
            "trading_mode": mode,
            "environment": environment,
            "order_preview": order_preview,
            "tp_sl": tp_sl,
            "account_snapshot": account_snapshot,
            "positions": positions,
            "recent_orders": recent_orders,
            "open_orders": open_orders,
            "conditional_orders": conditional_orders,
            "market_snapshot": {
                "symbol": symbol,
                "current_price": current_price,
            },
            "product_facts": product_facts,
            "partial": partial,
            "degraded_reasons": degraded_reasons,
            "constraints": constraints,
        }

    def collect_account_facts_payload(
        self,
        *,
        profile_name: str,
        market: str,
        trading_mode: str = DEFAULT_TRADING_MODE,
        symbol: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Collect internal account facts for a guarded order decision.

        This method is deliberately not a user-facing account scan or
        analysis endpoint; callers are the Trader guard and auto-trade runtime.
        """
        normalized_market = _validate_market(market)
        mode = _validate_trading_mode_market(trading_mode, normalized_market)
        environment = _environment_for_trading_mode(mode, normalized_market)
        if normalized_market == "all":
            raise AggregationInputError("account facts require a concrete market, not 'all'.")

        normalized_symbol = str(symbol).strip().upper() if symbol else None
        partial = False
        degraded_reasons: list[str] = []
        constraints: list[dict[str, Any]] = []
        end_ms = int(time.time() * 1000)
        start_ms = max(0, end_ms - RECENT_ORDER_LOOKBACK_MS)

        balances: list[dict[str, Any]]
        positions: list[dict[str, Any]]
        recent_orders: list[dict[str, Any]]
        open_orders: list[dict[str, Any]]
        conditional_orders: list[dict[str, Any]] = []
        current_price: float | None = None
        product_facts: dict[str, Any] | None = None
        market_snapshot_symbol = normalized_symbol

        if normalized_market == "futures":
            balances = _normalize_balance_entries(
                self.fetcher.fetch_futures_balance(profile_name=profile_name, trading_mode=mode),
                "futures",
                trading_mode=mode,
            )
            if not balances:
                partial = True
                _merge_degraded_reasons(degraded_reasons, ["futures_balance_unavailable"])
            positions = _normalize_positions(
                self.fetcher.fetch_futures_positions(profile_name=profile_name, trading_mode=mode),
                "futures",
                trading_mode=mode,
            )
            recent_orders, recent_orders_partial = self._collect_recent_futures_orders(
                profile_name=profile_name,
                trading_mode=mode,
                start_ms=start_ms,
                end_ms=end_ms,
                symbol=normalized_symbol,
                degraded_reasons=degraded_reasons,
                constraints=constraints,
            )
            partial = partial or recent_orders_partial
            open_orders = _normalize_orders(
                self.fetcher.fetch_futures_open_orders(
                    profile_name=profile_name,
                    symbol=normalized_symbol,
                ),
                "futures",
            )
            conditional_orders = _normalize_orders(
                self.fetcher.fetch_futures_pending_orders(
                    profile_name=profile_name,
                    symbol=normalized_symbol,
                ),
                "futures",
            )
            if not market_snapshot_symbol:
                market_snapshot_symbol = _pick_primary_futures_symbol(
                    positions=positions,
                    recent_orders=recent_orders,
                    open_orders=open_orders,
                    conditional_orders=conditional_orders,
                )
            if market_snapshot_symbol:
                fetch_product = getattr(self.fetcher, "fetch_futures_product_info", None)
                if callable(fetch_product):
                    try:
                        product_facts = _extract_symbol_product_info(
                            fetch_product(symbol=market_snapshot_symbol), market_snapshot_symbol
                        )
                    except Exception:
                        product_facts = None
                if product_facts is None:
                    partial = True
                    _merge_degraded_reasons(degraded_reasons, ["futures_product_rules_unavailable"])
                current_price = _safe_current_price(
                    fetch_latest_price=lambda: self.fetcher.fetch_futures_latest_price(symbol=market_snapshot_symbol),
                    market="futures",
                    symbol=market_snapshot_symbol,
                    degraded_reasons=degraded_reasons,
                    fetch_klines=lambda: self.fetcher.fetch_futures_klines(
                        symbol=market_snapshot_symbol,
                        start_ms=start_ms,
                        end_ms=end_ms,
                    ),
                )
                if current_price is None:
                    current_price = _estimate_futures_position_price(
                        positions=positions,
                        symbol=market_snapshot_symbol,
                    )
                    if current_price is not None:
                        _mark_market_snapshot_estimated(
                            degraded_reasons=degraded_reasons,
                            market="futures",
                            reason="futures_market_snapshot_estimated_from_position",
                        )
                if current_price is None:
                    partial = True
                else:
                    _apply_futures_position_price(
                        positions=positions,
                        symbol=market_snapshot_symbol,
                        current_price=current_price,
                    )
        else:
            balances, spot_balance_partial = self._collect_spot_balances(
                profile_name=profile_name,
                degraded_reasons=degraded_reasons,
            )
            partial = partial or spot_balance_partial
            positions = []
            recent_orders, recent_orders_partial = self._collect_recent_spot_orders(
                profile_name=profile_name,
                start_ms=start_ms,
                end_ms=end_ms,
                symbol=normalized_symbol,
                degraded_reasons=degraded_reasons,
                constraints=constraints,
            )
            partial = partial or recent_orders_partial
            open_orders = _normalize_orders(
                self.fetcher.fetch_spot_open_orders(
                    profile_name=profile_name,
                    symbol=normalized_symbol,
                ),
                "spot",
            )
            if normalized_symbol:
                fetch_product = getattr(self.fetcher, "fetch_spot_product_info", None)
                if callable(fetch_product):
                    try:
                        product_facts = _extract_symbol_product_info(
                            fetch_product(symbol=normalized_symbol), normalized_symbol
                        )
                    except Exception:
                        product_facts = None
                if product_facts is None:
                    partial = True
                    _merge_degraded_reasons(degraded_reasons, ["spot_product_rules_unavailable"])
                current_price = _safe_current_price(
                    fetch_latest_price=lambda: self.fetcher.fetch_spot_latest_price(
                        profile_name=profile_name,
                        symbol=normalized_symbol,
                    ),
                    market="spot",
                    symbol=normalized_symbol,
                    degraded_reasons=degraded_reasons,
                    fetch_klines=lambda: self.fetcher.fetch_spot_klines(
                        profile_name=profile_name,
                        symbol=normalized_symbol,
                    ),
                )
            _merge_degraded_reasons(degraded_reasons, ["spot_tp_sl_state_unavailable"])

        primary_balance = None
        if balances:
            primary_balance = next((row for row in balances if row.get("asset") == "USDT"), balances[0])
        if normalized_market == "spot":
            account_snapshot, positions = _build_spot_account_estimates(
                balances=balances,
                symbol=normalized_symbol,
                fetch_spot_latest_price=lambda *, symbol: self.fetcher.fetch_spot_latest_price(
                    profile_name=profile_name,
                    symbol=symbol,
                ),
                degraded_reasons=degraded_reasons,
            )
        else:
            account_snapshot = {
                "account_scope": "personal_futures",
                "equity": primary_balance.get("equity") if primary_balance else None,
                "available_balance": primary_balance.get("available_balance") if primary_balance else None,
            }

        return {
            # Internal account facts for a guarded order/authorization check;
            # this is not an independent account-risk scan interface.
            "context": "account_facts",
            "trading_mode": mode,
            "environment": environment,
            "market": normalized_market,
            "symbol": normalized_symbol,
            "account_snapshot": account_snapshot,
            "positions": positions,
            "recent_orders": recent_orders,
            "open_orders": open_orders,
            "conditional_orders": conditional_orders,
            "market_snapshot": {
                "symbol": market_snapshot_symbol,
                "current_price": current_price,
            },
            "product_facts": product_facts,
            "partial": partial,
            "degraded_reasons": degraded_reasons,
            "constraints": constraints,
        }


class WeexApiFetcher:
    def _contract_module(self) -> Any:
        import weex_contract_api as contract_api

        return contract_api

    def _spot_module(self) -> Any:
        import weex_spot_api as spot_api

        return spot_api

    def _build_contract_client(self, profile_name: str | None) -> tuple[Any, Any]:
        contract_api = self._contract_module()
        contract_api.refresh_agent_records(command="trade-aggregator.contract")
        if profile_name not in (None, ""):
            raise AggregationInputError("saved profiles are not supported; use WEEX environment credentials")
        environment_account = contract_api.load_environment_account()
        environment_validation = contract_api.validate_runtime_environment()
        if not environment_validation["ok"]:
            raise SystemExit(
                "Invalid runtime environment:\n"
                + "\n".join(f"- {issue}" for issue in environment_validation["issues"])
            )
        contract_api.ensure_private_runtime_ready(
            command="trade-aggregator.contract",
            auto_setup=True,
        )
        env_base_url = os.getenv("WEEX_CONTRACT_API_BASE") or os.getenv("WEEX_API_BASE")
        base_url = env_base_url or contract_api.DEFAULT_BASE_URL
        locale = os.getenv("WEEX_LOCALE") or contract_api.DEFAULT_LOCALE
        timeout = float(os.getenv("WEEX_API_TIMEOUT", contract_api.DEFAULT_TIMEOUT))
        client = contract_api.WeexContractClient(
            base_url=base_url,
            timeout=timeout,
            locale=locale,
            api_key=environment_account.credentials.api_key,
            api_secret=environment_account.credentials.api_secret,
            api_passphrase=environment_account.credentials.api_passphrase,
        )
        return contract_api, client

    def _build_public_contract_client(self) -> tuple[Any, Any]:
        contract_api = self._contract_module()
        contract_api.refresh_agent_records(command="trade-aggregator.contract.public")
        env_base_url = os.getenv("WEEX_CONTRACT_API_BASE") or os.getenv("WEEX_API_BASE")
        base_url = env_base_url or contract_api.DEFAULT_BASE_URL
        locale = os.getenv("WEEX_LOCALE") or contract_api.DEFAULT_LOCALE
        timeout = float(os.getenv("WEEX_API_TIMEOUT", contract_api.DEFAULT_TIMEOUT))
        client = contract_api.WeexContractClient(
            base_url=base_url,
            timeout=timeout,
            locale=locale,
            api_key=None,
            api_secret=None,
            api_passphrase=None,
        )
        return contract_api, client

    def _build_spot_client(self, profile_name: str | None) -> tuple[Any, Any]:
        spot_api = self._spot_module()
        spot_api.refresh_agent_records(command="trade-aggregator.spot")
        if profile_name not in (None, ""):
            raise AggregationInputError("saved profiles are not supported; use WEEX environment credentials")
        environment_account = spot_api.load_environment_account()
        environment_validation = spot_api.validate_runtime_environment()
        if not environment_validation["ok"]:
            raise SystemExit(
                "Invalid runtime environment:\n"
                + "\n".join(f"- {issue}" for issue in environment_validation["issues"])
            )
        spot_api.ensure_private_runtime_ready(
            command="trade-aggregator.spot",
            auto_setup=True,
        )
        env_base_url = os.getenv("WEEX_SPOT_API_BASE") or os.getenv("WEEX_API_BASE")
        base_url = env_base_url or spot_api.DEFAULT_BASE_URL
        locale = os.getenv("WEEX_LOCALE") or spot_api.DEFAULT_LOCALE
        timeout = float(os.getenv("WEEX_API_TIMEOUT", spot_api.DEFAULT_TIMEOUT))
        client = spot_api.WeexSpotClient(
            base_url=base_url,
            timeout=timeout,
            locale=locale,
            api_key=environment_account.credentials.api_key,
            api_secret=environment_account.credentials.api_secret,
            api_passphrase=environment_account.credentials.api_passphrase,
        )
        return spot_api, client

    def _build_public_spot_client(self, profile_name: str = "") -> tuple[Any, Any]:
        spot_api = self._spot_module()
        spot_api.refresh_agent_records(command="trade-aggregator.spot.public")
        if profile_name:
            raise AggregationInputError("saved profiles are not supported; use WEEX environment configuration")
        env_base_url = os.getenv("WEEX_SPOT_API_BASE") or os.getenv("WEEX_API_BASE")
        base_url = env_base_url or spot_api.DEFAULT_BASE_URL
        locale = os.getenv("WEEX_LOCALE") or spot_api.DEFAULT_LOCALE
        timeout = float(os.getenv("WEEX_API_TIMEOUT", spot_api.DEFAULT_TIMEOUT))
        client = spot_api.WeexSpotClient(
            base_url=base_url,
            timeout=timeout,
            locale=locale,
            api_key=None,
            api_secret=None,
            api_passphrase=None,
        )
        return spot_api, client

    def _send_contract_request(
        self,
        *,
        profile_name: str,
        endpoint_key: str,
        query: dict[str, Any],
        body: dict[str, Any] | None = None,
        public: bool = False,
        trading_mode: str = DEFAULT_TRADING_MODE,
    ) -> Any:
        if public:
            contract_api, client = self._build_public_contract_client()
        else:
            contract_api, client = self._build_contract_client(profile_name)
        endpoint = contract_api.ENDPOINTS[endpoint_key]
        contract_api.validate_endpoint_trading_mode(endpoint, trading_mode)
        prepared = client.prepare_request(endpoint, query=query, body=body or {})
        response = client.send(prepared)
        if not response.get("ok"):
            raise AggregationInputError(
                f"Contract request failed for {endpoint_key}: {response.get('error')}"
            )
        return response.get("data")

    def _send_spot_request(
        self,
        *,
        profile_name: str,
        endpoint_key: str,
        query: dict[str, Any],
        body: dict[str, Any] | None = None,
        public: bool = False,
    ) -> Any:
        if public:
            spot_api, client = self._build_public_spot_client(profile_name)
        else:
            spot_api, client = self._build_spot_client(profile_name)
        endpoint = spot_api.ENDPOINTS[endpoint_key]
        prepared = client.prepare_request(endpoint, query=query, body=body or {})
        response = client.send(prepared)
        if not response.get("ok"):
            raise AggregationInputError(
                f"Spot request failed for {endpoint_key}: {response.get('error')}"
            )
        return response.get("data")

    def fetch_futures_balance(
        self,
        *,
        profile_name: str,
        trading_mode: str = DEFAULT_TRADING_MODE,
    ) -> Any:
        _normalize_trading_mode(trading_mode)
        kwargs: dict[str, Any] = {
            "profile_name": profile_name,
            "endpoint_key": "account.get_account_balance",
            "query": {},
        }
        return self._send_contract_request(**kwargs)

    def fetch_futures_positions(
        self,
        *,
        profile_name: str,
        trading_mode: str = DEFAULT_TRADING_MODE,
    ) -> Any:
        _normalize_trading_mode(trading_mode)
        kwargs: dict[str, Any] = {
            "profile_name": profile_name,
            "endpoint_key": "account.get_all_positions",
            "query": {},
        }
        return self._send_contract_request(**kwargs)

    def fetch_futures_orders(
        self,
        *,
        profile_name: str,
        trading_mode: str = DEFAULT_TRADING_MODE,
        start_ms: int,
        end_ms: int,
        symbol: str | None,
    ) -> Any:
        _normalize_trading_mode(trading_mode)
        endpoint_key = "transaction.get_order_history"
        normalized_symbol = str(symbol or "").strip().upper() or None
        upstream_symbol = normalized_symbol
        rows: list[dict[str, Any]] = []
        for window in split_time_range(start_ms, end_ms, max_span_days=MAX_FUTURES_WINDOW_DAYS):
            page = 0
            while True:
                query: dict[str, Any] = {
                    "startTime": window.start_ms,
                    "endTime": window.end_ms,
                    "limit": FUTURES_ORDER_LIMIT,
                    "page": page,
                }
                if upstream_symbol:
                    query["symbol"] = upstream_symbol
                kwargs: dict[str, Any] = {
                    "profile_name": profile_name,
                    "endpoint_key": endpoint_key,
                    "query": query,
                }
                payload = self._send_contract_request(**kwargs)
                page_rows = _extract_list_payload(payload, "items", "orders")
                if not page_rows:
                    break
                _extend_unique_dict_rows(
                    rows,
                    page_rows,
                    identity_keys=("orderId", "clientOrderId", "time", "symbol"),
                )
                if len(page_rows) < FUTURES_ORDER_LIMIT:
                    break
                page += 1
        return rows

    def fetch_futures_klines(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> Any:
        rows: list[list[Any]] = []
        cursor = start_ms
        chunk_ms = KLINE_LIMIT * HOUR_MS
        while cursor <= end_ms:
            chunk_end = min(end_ms, cursor + chunk_ms - 1)
            payload = self._send_contract_request(
                profile_name="",
                endpoint_key="market.get_history_klines",
                query={
                    "symbol": symbol,
                    "interval": "1h",
                    "startTime": cursor,
                    "endTime": chunk_end,
                    "limit": KLINE_LIMIT,
                    "priceType": "LAST",
                },
                public=True,
            )
            if isinstance(payload, list):
                rows.extend(payload)
            cursor = chunk_end + 1
        return rows

    def fetch_spot_balance(self, *, profile_name: str) -> Any:
        return self._send_spot_request(
            profile_name=profile_name,
            endpoint_key="spot.account.get_account_balance",
            query={},
        )

    def fetch_spot_latest_price(self, *, profile_name: str = "", symbol: str) -> Any:
        return self._send_spot_request(
            profile_name=profile_name,
            endpoint_key="spot.market.get_ticker_info",
            query={"symbol": symbol},
            public=True,
        )

    def fetch_spot_orders(
        self,
        *,
        profile_name: str,
        start_ms: int,
        end_ms: int,
        symbol: str | None,
    ) -> Any:
        if not symbol:
            return []
        rows: list[dict[str, Any]] = []
        for window in split_time_range(start_ms, end_ms, max_span_days=MAX_SPOT_HISTORY_WINDOW_DAYS):
            page = 1
            request_limit = SPOT_ORDER_LIMIT
            while True:
                try:
                    payload = self._send_spot_request(
                        profile_name=profile_name,
                        endpoint_key="spot.order.history_orders",
                        query={
                            "symbol": symbol,
                            "startTime": window.start_ms,
                            "endTime": window.end_ms,
                            "limit": request_limit,
                            "page": page,
                        },
                    )
                except AggregationInputError as exc:
                    if page == 1 and _should_retry_spot_history_orders_with_safe_limit(exc, limit=request_limit):
                        request_limit = SPOT_ORDER_SAFE_LIMIT
                        continue
                    raise
                page_rows = _extract_list_payload(payload, "items", "orders")
                if not page_rows:
                    break
                _extend_unique_dict_rows(rows, page_rows, identity_keys=("orderId", "clientOrderId", "time", "symbol"))
                if len(page_rows) < request_limit:
                    break
                page += 1
        return rows

    def fetch_spot_klines(
        self,
        *,
        profile_name: str = "",
        symbol: str,
    ) -> Any:
        return self._send_spot_request(
            profile_name=profile_name,
            endpoint_key="spot.market.get_k_line_data",
            query={
                "symbol": symbol,
                "interval": "1h",
            },
            public=True,
        )

    def fetch_futures_latest_price(self, *, symbol: str) -> Any:
        return self._send_contract_request(
            profile_name="",
            endpoint_key="market.get_symbol_price",
            query={
                "symbol": symbol,
                "priceType": "MARK",
            },
            public=True,
        )

    def fetch_futures_product_info(self, *, symbol: str) -> Any:
        return self._send_contract_request(
            profile_name="",
            endpoint_key="market.get_contract_info",
            query={"symbol": symbol},
            public=True,
        )

    def fetch_spot_product_info(self, *, symbol: str) -> Any:
        return self._send_spot_request(
            profile_name="",
            endpoint_key="spot.config.get_product_info",
            query={"symbol": symbol},
            public=True,
        )

    def fetch_futures_open_orders(
        self,
        *,
        profile_name: str,
        symbol: str | None,
    ) -> Any:
        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            query: dict[str, Any] = {
                "limit": FUTURES_OPEN_ORDER_LIMIT,
                "page": page,
            }
            if symbol:
                query["symbol"] = symbol
            payload = self._send_contract_request(
                profile_name=profile_name,
                endpoint_key="transaction.get_current_order_status",
                query=query,
            )
            page_rows = _extract_list_payload(payload, "items", "orders")
            if not page_rows:
                break
            _extend_unique_dict_rows(
                rows,
                page_rows,
                identity_keys=("orderId", "clientOrderId", "time", "symbol"),
            )
            if len(page_rows) < FUTURES_OPEN_ORDER_LIMIT:
                break
            page += 1
        return rows

    def fetch_futures_pending_orders(
        self,
        *,
        profile_name: str,
        symbol: str | None,
    ) -> Any:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            query: dict[str, Any] = {
                "page": page,
                "limit": FUTURES_PENDING_LIMIT,
            }
            if symbol:
                query["symbol"] = symbol
            payload = self._send_contract_request(
                profile_name=profile_name,
                endpoint_key="transaction.get_current_pending_orders",
                query=query,
            )
            page_rows = _extract_list_payload(payload, "items", "orders")
            if not page_rows:
                break
            _extend_unique_dict_rows(rows, page_rows, identity_keys=("algoId", "actualOrderId", "createTime", "symbol"))
            if len(page_rows) < FUTURES_PENDING_LIMIT:
                break
            page += 1
        return rows

    def fetch_spot_open_orders(
        self,
        *,
        profile_name: str,
        symbol: str | None,
    ) -> Any:
        query: dict[str, Any] = {}
        if symbol:
            query["symbol"] = symbol
        return self._send_spot_request(
            profile_name=profile_name,
            endpoint_key="spot.order.unfinished_orders",
            query=query,
        )


def _output_json(payload: dict[str, Any], pretty: bool) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))


def _output_error(error: str, pretty: bool) -> None:
    _output_json({"ok": False, "error": error}, pretty)


def _arg_value(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return vars(args).get(name, default)


def _parse_order_json(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --order-json payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("--order-json must decode to a JSON object.")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect internal order-risk data for the WEEX Trader guard."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        raise SystemExit(f"Unsupported command: {args.command}")
    except AggregationInputError as exc:
        _output_error(str(exc), bool(getattr(args, "pretty", False)))
        return 1


__all__ = [
    "AggregationInputError",
    "TimeWindow",
    "TradeDataAggregator",
    "WeexApiFetcher",
    "build_parser",
    "main",
    "split_time_range",
]


if __name__ == "__main__":
    raise SystemExit(main())
