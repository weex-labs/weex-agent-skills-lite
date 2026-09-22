from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_contract_api  # noqa: E402
import weex_order_intent_state  # noqa: E402
import weex_trade_guard  # noqa: E402
import weex_trade_data_aggregator  # noqa: E402
import weex_spot_api  # noqa: E402
import weex_message_templates  # noqa: E402


class TradeGuardRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "WEEX_API_KEY": "test-api-key",
            "WEEX_API_SECRET": "test-api-secret",
            "WEEX_API_PASSPHRASE": "test-api-passphrase",
        }
        self.environment_patch = mock.patch.dict(os.environ, self.environment, clear=False)
        self.environment_patch.start()
        self.account_id = weex_trade_guard._current_environment_account_id()

    def tearDown(self) -> None:
        self.environment_patch.stop()

    def test_trade_guard_language_gate_maps_supported_input_to_locale(self) -> None:
        args = weex_trade_guard.build_parser().parse_args(
            [
                "preview-order",
                "--market",
                "spot",
                "--order-json",
                "{}",
                "--input-language",
                "ja",
            ]
        )
        weex_trade_guard._resolve_cli_language(args)
        self.assertEqual(args.language, "ja")
        self.assertEqual(args.language_decision.source, "detected")
        self.assertIsNone(args.language_decision.fallback_reason)

    def test_trade_guard_language_gate_rejects_conflicting_render_language(self) -> None:
        args = weex_trade_guard.build_parser().parse_args(
            [
                "preview-order",
                "--market",
                "spot",
                "--order-json",
                "{}",
                "--input-language",
                "ja",
                "--language",
                "zh",
            ]
        )
        with self.assertRaises(weex_trade_guard.AggregationInputError):
            weex_trade_guard._resolve_cli_language(args)

    def test_trade_guard_language_gate_requires_a_language_signal(self) -> None:
        args = weex_trade_guard.build_parser().parse_args(
            ["preview-order", "--market", "spot", "--order-json", "{}"]
        )
        with self.assertRaises(weex_trade_guard.AggregationInputError):
            weex_trade_guard._resolve_cli_language(args)

    def test_confirmation_rejects_language_change_after_preview(self) -> None:
        intent = {"confirmation_language": "en"}
        self.assertFalse(
            weex_trade_guard._intent_language_matches(
                intent,
                argparse.Namespace(language="zh"),
            )
        )
        self.assertTrue(
            weex_trade_guard._intent_language_matches(
                intent,
                argparse.Namespace(language="en"),
            )
        )

    @staticmethod
    def _spot_preview_payload_from_raw(raw_order: dict[str, object]) -> dict[str, object]:
        order_type = str(raw_order.get("order_type") or raw_order.get("type") or "").upper()
        return {
            "environment": {"trading_mode": "live", "market": "spot", "uses_real_funds": True},
            "order_preview": {
                "market": "spot",
                "symbol": raw_order.get("symbol"),
                "side": str(raw_order.get("side") or "").upper(),
                "position_side": None,
                "order_type": order_type,
                "quantity": float(raw_order["quantity"]),
                "price": None,
                "time_in_force": None,
            },
            "product_facts": {
                "status": "TRADING",
                "enableTrade": True,
                "stepSize": "0.000001",
                "minTradeAmount": "0.000001",
                "maxTradeAmount": "100",
            },
            "account_snapshot": {"quote_available_balance": "100"},
            "positions": [],
            "recent_orders": [],
            "open_orders": [],
            "conditional_orders": [],
            "market_snapshot": {"symbol": "BTCUSDT", "current_price": 78300},
            "tp_sl": {"has_take_profit": False, "has_stop_loss": False},
            "partial": False,
            "degraded_reasons": ["spot_tp_sl_state_unavailable"],
            "constraints": [],
        }

    def test_manual_order_validation_rejects_missing_and_invalid_fields(self) -> None:
        missing = weex_trade_guard.validate_manual_order(
            "futures", {"side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": "1"}
        )
        self.assertTrue(any("symbol" in item for item in missing))

        invalid = weex_trade_guard.validate_manual_order(
            "futures",
            {
                "symbol": "BTCUSDT",
                "side": "HOLD",
                "positionSide": "SIDEWAYS",
                "type": "LIMIT",
                "quantity": "0",
            },
        )
        self.assertGreaterEqual(len(invalid), 4)
        self.assertTrue(any("price" in item for item in invalid))

    def test_preview_rejects_conflicting_order_type_aliases_before_aggregation(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="spot",
            trading_mode="live",
            order_json=(
                '{"symbol":"BTCUSDT","side":"BUY","orderType":"MARKET",'
                '"type":"LIMIT","quantity":"0.000253"}'
            ),
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )
        with mock.patch.object(weex_trade_guard, "TradeDataAggregator") as aggregator, mock.patch(
            "sys.stdout.write", side_effect=lambda value: None
        ):
            self.assertEqual(weex_trade_guard.cmd_preview_order(args), 1)
        aggregator.assert_not_called()

    def test_product_rules_reject_minimum_step_and_tick_mismatches(self) -> None:
        facts = {
            "status": "TRADING",
            "stepSize": "0.001",
            "minTradeAmount": "0.001",
            "maxTradeAmount": "10",
            "tickSize": "0.1",
        }
        with self.assertRaises(weex_trade_guard.AggregationInputError):
            weex_trade_guard._validate_product_rules("spot", {"quantity": "0.0005"}, facts)
        with self.assertRaises(weex_trade_guard.AggregationInputError):
            weex_trade_guard._validate_product_rules("spot", {"quantity": "0.001", "price": "1.01"}, facts)

    def test_conditional_order_routes_to_official_pending_endpoint_and_keeps_trigger(self) -> None:
        class Endpoint:
            def __init__(self, key: str) -> None:
                self.key = key

        captured: dict[str, object] = {}

        class Client:
            def prepare_request(self, endpoint, query, body):
                captured["endpoint"] = endpoint.key
                captured["body"] = body
                return {"endpoint": endpoint.key, "body": body}

            def send(self, prepared):
                return {"ok": True, "status": 200, "data": {"orderId": "1"}}

        api = types.SimpleNamespace(
            ENDPOINTS={
                "transaction.place_order": Endpoint("transaction.place_order"),
                "transaction.place_pending_order": Endpoint("transaction.place_pending_order"),
            },
            normalize_contract_trade_symbol=lambda value: value,
            validate_endpoint_trading_mode=lambda endpoint, mode: mode,
            generate_client_oid=lambda: "oid",
        )
        with mock.patch.object(weex_trade_guard, "_build_contract_client", return_value=(api, Client())):
            result = weex_trade_guard._submit_order(
                market="futures",
                trading_mode="live",
                raw_order={
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "positionSide": "LONG",
                    "type": "STOP_MARKET",
                    "quantity": "1",
                    "triggerPrice": "70000",
                    "workingType": "MARK_PRICE",
                },
                language="en",
            )
        self.assertEqual(result["orderId"], "1")
        self.assertEqual(captured["endpoint"], "transaction.place_pending_order")
        self.assertEqual(captured["body"]["triggerPrice"], "70000")

    def test_exact_position_close_uses_existing_payload_helper(self) -> None:
        self.assertTrue(hasattr(weex_contract_api, "execute_endpoint_payload"))

    def test_exact_position_close_submits_close_positions_endpoint(self) -> None:
        class Endpoint:
            key = "transaction.close_positions"

        api = types.SimpleNamespace(
            ENDPOINTS={"transaction.close_positions": Endpoint()},
            find_endpoint_key_by_doc_suffix=lambda suffix: "transaction.close_positions",
            normalize_contract_trade_symbol=lambda value: value,
            execute_endpoint_payload=lambda **kwargs: (0, {"ok": True, "result": {"orderId": "close-1"}}),
        )
        aggregator = types.SimpleNamespace(
            collect_account_facts_payload=lambda **kwargs: {
                "partial": False,
                "degraded_reasons": [],
                "positions": [{"position_id": "7", "symbol": "BTCUSDT", "position_side": "LONG", "quantity": "1"}],
            }
        )
        order = {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "LONG",
            "type": "MARKET",
            "quantity": "1",
            "position_id": "7",
        }
        with mock.patch.object(weex_trade_guard, "TradeDataAggregator", return_value=aggregator), mock.patch.object(
            weex_trade_guard, "_build_contract_client", return_value=(api, object())
        ):
            result = weex_trade_guard._submit_order(
                market="futures", trading_mode="live", raw_order=order, language="en"
            )
        self.assertEqual(result["orderId"], "close-1")

    def test_tp_sl_normalization_generates_client_id_and_validates_execute_price(self) -> None:
        normalized = weex_trade_guard._normalize_tp_sl_order(
            {
                "symbol": "BTCUSDT",
                "planType": "TAKE_PROFIT",
                "triggerPrice": "90000",
                "positionSide": "LONG",
            }
        )
        self.assertTrue(normalized["clientAlgoId"])
        with self.assertRaises(weex_trade_guard.AggregationInputError):
            weex_trade_guard._normalize_tp_sl_order(
                {
                    "symbol": "BTCUSDT",
                    "planType": "TAKE_PROFIT",
                    "triggerPrice": "90000",
                    "executePrice": "abc",
                    "positionSide": "LONG",
                }
            )

    def test_tp_sl_confirmation_contains_order_context_without_risk_prompt(self) -> None:
        confirmation = weex_trade_guard._build_user_confirmation(
            "zh",
            environment={"trading_mode": "live", "market": "futures", "uses_real_funds": True},
            preview_context={
                "order_preview": {
                    "market": "futures",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "position_side": "LONG",
                    "order_type": "TAKE_PROFIT",
                    "quantity": "1",
                    "price": "0",
                    "trigger_price": "90000",
                },
                "alerts": [{"type": "missing_tp_sl", "level": "high"}],
            },
        )
        self.assertIn("BTCUSDT", confirmation["reply_instruction"])
        self.assertIn("90000", confirmation["reply_instruction"])
        self.assertNotIn("风险提示", confirmation["reply_instruction"])

    def test_confirmation_instruction_is_confirmation_only_and_keeps_fixed_auto_auth_hint(self) -> None:
        confirmation = weex_trade_guard._build_user_confirmation(
            "zh",
            environment={"trading_mode": "live", "market": "futures", "uses_real_funds": True},
            preview_context={
                "order_preview": {
                    "market": "futures",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "position_side": "LONG",
                    "order_type": "MARKET",
                    "quantity": "1",
                },
                "alerts": [{"type": "missing_tp_sl", "level": "high"}],
            },
            include_mode_switch=True,
            include_auto_trade_authorization_hint=True,
        )
        text = confirmation["reply_instruction"]
        self.assertNotIn("风险提示", text)
        self.assertNotIn("risk alert", text.lower())
        self.assertIn("如果确认使用真实资金提交这笔订单，请回复：确认", text)
        self.assertTrue(
            text.endswith(
                "如需取消二次确认功能，可申请自动交易授权。授权后，在指定交易类型、交易对、"
                "单笔金额和有效期范围内，下单无需逐笔确认。发送“申请自动交易授权”即可开始配置。"
            )
        )

    def test_market_confirmation_uses_concise_price_notice(self) -> None:
        confirmation = weex_trade_guard._build_user_confirmation(
            "zh",
            environment={"trading_mode": "live", "market": "spot", "uses_real_funds": True},
            preview_context={
                "order_preview": {
                    "market": "spot",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity": "0.000253",
                }
            },
            market_price_recheck_skipped=True,
        )
        text = confirmation["reply_instruction"]
        self.assertIn(
            "价格提示：实际成交价可能随市场波动，请以 WEEX 最终成交结果为准。",
            text,
        )
        self.assertNotIn("滑点", text)
        self.assertNotIn("确认后系统不会再次", text)
        english = weex_trade_guard._build_user_confirmation(
            "en",
            environment={"trading_mode": "live", "market": "spot", "uses_real_funds": True},
            preview_context={
                "order_preview": {
                    "market": "spot",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity": "0.000253",
                }
            },
            market_price_recheck_skipped=True,
        )["reply_instruction"]
        self.assertIn(
            "Price notice: The actual execution price may fluctuate with the market. "
            "Please refer to the final WEEX execution result.",
            english,
        )
        limit_text = weex_trade_guard._build_user_confirmation(
            "zh",
            environment={"trading_mode": "live", "market": "spot", "uses_real_funds": True},
            preview_context={
                "order_preview": {
                    "market": "spot",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "quantity": "0.000253",
                    "price": "78000",
                }
            },
        )["reply_instruction"]
        self.assertNotIn("价格提示", limit_text)

    def test_market_confirm_submits_signed_preview_order_without_refreshing_price_facts(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="spot",
            trading_mode="live",
            order_json='{"symbol":"BTCUSDT","side":"BUY","type":"MARKET","quantity":"0.000253"}',
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )
        payload = self._spot_preview_payload_from_raw(json.loads(args.order_json))
        fake = types.SimpleNamespace(collect_order_risk_payload=mock.Mock(return_value=payload))
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False
        ), mock.patch.object(
            weex_trade_guard, "TradeDataAggregator", return_value=fake
        ), mock.patch("sys.stdout.write", side_effect=lambda value: None):
            self.assertEqual(weex_trade_guard.cmd_preview_order(args, now_ms=1000), 0)
            intent = weex_order_intent_state.load_intent()
            self.assertFalse(intent["freshness_required"])
            confirm_args = argparse.Namespace(
                intent_id=intent["intent_id"],
                risk_signature=intent["risk_signature"],
                trading_mode="live",
                confirm_live=True,
                user_reply="确认",
                profile="profile",
                language="zh",
                pretty=False,
            )
            with mock.patch.object(weex_trade_guard, "TradeDataAggregator") as refresh, mock.patch.object(
                weex_trade_guard, "_submit_live_order", return_value={"orderId": "spot-1"}
            ) as submitter:
                self.assertEqual(weex_trade_guard.cmd_confirm_order(confirm_args, now_ms=1001), 0)
            refresh.assert_not_called()
            submitter.assert_called_once()
            self.assertEqual(submitter.call_args.kwargs["raw_order"], intent["raw_order"])

    def test_order_type_alias_uses_the_same_market_warning_and_freshness_classification(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="spot",
            trading_mode="live",
            order_json='{"symbol":"BTCUSDT","side":"BUY","orderType":"MARKET","quantity":"0.000253"}',
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )

        class FakeAggregator:
            def collect_order_risk_payload(self, **kwargs):
                return TradeGuardRegressionTests._spot_preview_payload_from_raw(kwargs["raw_order"])

        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False
        ), mock.patch.object(
            weex_trade_guard, "TradeDataAggregator", return_value=FakeAggregator()
        ):
            output: list[str] = []
            with mock.patch("sys.stdout.write", side_effect=lambda value: output.append(value)):
                self.assertEqual(weex_trade_guard.cmd_preview_order(args, now_ms=1000), 0)
            result = json.loads("".join(output))
            intent = weex_order_intent_state.load_intent()
        self.assertFalse(intent["freshness_required"])
        self.assertIn("type", intent["raw_order"])
        self.assertEqual(intent["raw_order"]["type"], "MARKET")
        self.assertIn(
            "价格提示：实际成交价可能随市场波动，请以 WEEX 最终成交结果为准。",
            result["user_confirmation"]["reply_instruction"],
        )

    def test_market_order_with_attached_tp_sl_keeps_fresh_fact_checks(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="spot",
            trading_mode="live",
            order_json=(
                '{"symbol":"BTCUSDT","side":"BUY","type":"MARKET","quantity":"0.000253",'
                '"tpTriggerPrice":"90000"}'
            ),
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )

        class FakeAggregator:
            def collect_order_risk_payload(self, **kwargs):
                return TradeGuardRegressionTests._spot_preview_payload_from_raw(kwargs["raw_order"])

        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False
        ), mock.patch.object(
            weex_trade_guard, "TradeDataAggregator", return_value=FakeAggregator()
        ):
            output: list[str] = []
            with mock.patch("sys.stdout.write", side_effect=lambda value: output.append(value)):
                self.assertEqual(weex_trade_guard.cmd_preview_order(args, now_ms=1000), 0)
            result = json.loads("".join(output))
            intent = weex_order_intent_state.load_intent()
        self.assertTrue(intent["freshness_required"])
        self.assertNotIn(
            "价格提示",
            result["user_confirmation"]["reply_instruction"],
        )

    def test_preview_order_public_payload_hides_risk_analysis_fields(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="futures",
            trading_mode="live",
            order_json='{"symbol":"BTCUSDT","side":"BUY","positionSide":"LONG","type":"MARKET","quantity":"1"}',
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )
        payload = {
            "environment": {"trading_mode": "live", "market": "futures", "uses_real_funds": True},
            "order_preview": {"market": "futures", "symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "order_type": "MARKET", "quantity": 1},
            "product_facts": {"status": "TRADING", "minOrderSize": "0.001", "maxOrderSize": "100", "quantityPrecision": 3},
            "partial": False,
            "degraded_reasons": [],
            "constraints": [],
        }
        fake = types.SimpleNamespace(collect_order_risk_payload=lambda **kwargs: payload)
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False), mock.patch.object(
            weex_trade_guard, "TradeDataAggregator", return_value=fake
        ):
            output = []
            with mock.patch("sys.stdout.write", side_effect=lambda value: output.append(value)):
                self.assertEqual(weex_trade_guard.cmd_preview_order(args, now_ms=1000), 0)
        import json
        result = json.loads("".join(output))
        for key in ("has_risk", "alerts", "disclaimer", "next_action_hint", "degraded_reasons"):
            self.assertNotIn(key, result)
        self.assertIn(
            "如需取消二次确认功能，可申请自动交易授权。授权后，在指定交易类型、交易对、"
            "单笔金额和有效期范围内，下单无需逐笔确认。发送“申请自动交易授权”即可开始配置。",
            result["user_confirmation"]["reply_instruction"],
        )
        self.assertTrue(result["user_confirmation"]["render_verbatim"])
        self.assertEqual(
            result["user_confirmation"]["reply_instruction_digest"],
            weex_message_templates.confirmation_instruction_digest(
                result["user_confirmation"]["reply_instruction"]
            ),
        )

    def test_confirm_requires_exact_user_reply_and_blocks_review_required_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            old_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
            os.environ["WEEX_TRADER_SKILL_HOME"] = tempdir
            try:
                intent = weex_order_intent_state.build_intent(
                    account_id=self.account_id,
                    market="futures",
                    trading_mode="live",
                    environment={"trading_mode": "live", "market": "futures", "uses_real_funds": True},
                    order_preview={"market": "futures", "symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "order_type": "MARKET", "quantity": "1"},
                    raw_order={"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": "1"},
                    analysis_output={"alerts": [], "partial": False, "degraded_reasons": [], "constraints": []},
                    confirmation_reply_text="确认",
                    confirmation_language="zh",
                    freshness_required=False,
                )
                weex_order_intent_state.save_intent(intent)
                args = argparse.Namespace(
                    intent_id=intent["intent_id"],
                    risk_signature=intent["risk_signature"],
                    trading_mode="live",
                    confirm_live=True,
                    user_reply="wrong",
                    profile="profile",
                    language="zh",
                    pretty=False,
                )
                with mock.patch.object(weex_trade_guard, "_submit_live_order", return_value={"orderId": "1"}) as submitter:
                    self.assertEqual(weex_trade_guard.cmd_confirm_order(args), 1)
                submitter.assert_not_called()
                self.assertTrue(weex_order_intent_state.load_intent())
                args.user_reply = "确认"
                with mock.patch.object(weex_trade_guard, "_submit_live_order", return_value={"orderId": "1"}) as submitter:
                    self.assertEqual(weex_trade_guard.cmd_confirm_order(args), 0)
                submitter.assert_called_once()
            finally:
                if old_home is None:
                    os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
                else:
                    os.environ["WEEX_TRADER_SKILL_HOME"] = old_home

    def test_confirm_rejects_when_environment_account_changed_after_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(os.environ, {"WEEX_TRADER_SKILL_HOME": tempdir}, clear=False):
                intent = weex_order_intent_state.build_intent(
                    account_id=self.account_id,
                    market="futures",
                    trading_mode="live",
                    environment={"trading_mode": "live", "market": "futures", "uses_real_funds": True},
                    order_preview={"market": "futures", "symbol": "BTCUSDT"},
                    raw_order={"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": "1"},
                    analysis_output={"alerts": [], "partial": False, "degraded_reasons": [], "constraints": []},
                    confirmation_reply_text="确认",
                    confirmation_language="zh",
                    freshness_required=False,
                )
                weex_order_intent_state.save_intent(intent)
                args = argparse.Namespace(
                    intent_id=intent["intent_id"],
                    risk_signature=intent["risk_signature"],
                    trading_mode="live",
                    confirm_live=True,
                    user_reply="确认",
                    language="zh",
                    pretty=False,
                )
                with mock.patch.dict(os.environ, {"WEEX_API_KEY": "different-api-key"}, clear=False):
                    with mock.patch.object(weex_trade_guard, "_submit_live_order") as submitter:
                        self.assertEqual(weex_trade_guard.cmd_confirm_order(args), 1)
                submitter.assert_not_called()

    def test_limit_confirm_still_rejects_changed_account_or_market_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            old_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
            os.environ["WEEX_TRADER_SKILL_HOME"] = tempdir
            try:
                args = argparse.Namespace(
                    profile="profile",
                    market="futures",
                    trading_mode="live",
                    order_json=(
                        '{"symbol":"BTCUSDT","side":"BUY","positionSide":"LONG",'
                        '"type":"LIMIT","quantity":"1","price":"100","timeInForce":"GTC"}'
                    ),
                    ttl_seconds=300,
                    language="zh",
                    pretty=False,
                )
                base_payload = {
                    "environment": {"trading_mode": "live", "market": "futures", "uses_real_funds": True},
                    "order_preview": {
                        "market": "futures",
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "position_side": "LONG",
                        "order_type": "LIMIT",
                        "quantity": 1,
                        "price": 100,
                        "time_in_force": "GTC",
                    },
                    "product_facts": {"status": "TRADING", "minOrderSize": "0.001", "maxOrderSize": "100", "quantityPrecision": 3},
                    "partial": False,
                    "degraded_reasons": [],
                    "constraints": [],
                    "positions": [],
                    "recent_orders": [],
                    "open_orders": [],
                    "conditional_orders": [],
                }
                payloads = [
                    {
                        **base_payload,
                        "account_snapshot": {"available_balance": 1000},
                        "market_snapshot": {"symbol": "BTCUSDT", "current_price": 100},
                    },
                    {
                        **base_payload,
                        "account_snapshot": {"available_balance": 0},
                        "market_snapshot": {"symbol": "BTCUSDT", "current_price": 101},
                    },
                ]

                class FakeAggregator:
                    def __init__(self) -> None:
                        self.calls = 0

                    def collect_order_risk_payload(self, **kwargs):
                        payload = payloads[min(self.calls, len(payloads) - 1)]
                        self.calls += 1
                        return payload

                fake = FakeAggregator()
                with mock.patch.object(weex_trade_guard, "TradeDataAggregator", return_value=fake), mock.patch(
                    "sys.stdout.write", side_effect=lambda value: None
                ):
                    self.assertEqual(weex_trade_guard.cmd_preview_order(args, now_ms=1000), 0)
                intent = weex_order_intent_state.load_intent()
                self.assertTrue(intent["freshness_required"])
                confirm_args = argparse.Namespace(
                    intent_id=intent["intent_id"],
                    risk_signature=intent["risk_signature"],
                    trading_mode="live",
                    confirm_live=True,
                    user_reply="确认",
                    profile="profile",
                    language="zh",
                    pretty=False,
                )
                with mock.patch.object(weex_trade_guard, "TradeDataAggregator", return_value=fake), mock.patch.object(
                    weex_trade_guard, "_submit_live_order"
                ) as submitter:
                    self.assertEqual(weex_trade_guard.cmd_confirm_order(confirm_args, now_ms=1001), 1)
                submitter.assert_not_called()
                self.assertEqual(fake.calls, 2)
            finally:
                if old_home is None:
                    os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
                else:
                    os.environ["WEEX_TRADER_SKILL_HOME"] = old_home

    def test_manual_auto_fallback_rechecks_official_facts_before_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            old_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
            os.environ["WEEX_TRADER_SKILL_HOME"] = tempdir
            try:
                order = {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": "1",
                    "price": "100",
                }
                intent = weex_order_intent_state.build_intent(
                    account_id=self.account_id,
                    market="spot",
                    trading_mode="live",
                    environment={"trading_mode": "live", "market": "spot", "uses_real_funds": True},
                    order_preview={"operation_key": "spot.order.place_order", "orders": [order]},
                    raw_order=order,
                    analysis_output={"alerts": [], "blocking_reasons": []},
                    confirmation_reply_text="确认",
                    confirmation_language="zh",
                    freshness_required=False,
                )
                intent.update(
                    {
                        "auto_fallback_operation_key": "spot.order.place_order",
                        "auto_fallback_orders": [order],
                    }
                )
                # The signature is intentionally recomputed after adding the
                # fallback metadata, matching how the facade persists it.
                intent["risk_signature"] = weex_order_intent_state.build_risk_signature(
                    account_id=intent["account_id"],
                    market=intent["market"],
                    trading_mode=intent["trading_mode"],
                    order_preview=intent["order_preview"],
                    raw_order=intent["raw_order"],
                    analysis_output=intent["analysis_output"],
                    intent_type=intent["intent_type"],
                    environment=intent["environment"],
                    intent_id=intent["intent_id"],
                    created_at=intent["created_at"],
                    expires_at=intent["expires_at"],
                    ttl_seconds=intent["ttl_seconds"],
                    confirmation_reply_text=intent["confirmation_reply_text"],
                    confirmation_language=intent["confirmation_language"],
                    freshness_required=intent["freshness_required"],
                )
                weex_order_intent_state.save_intent(intent)
                args = argparse.Namespace(
                    intent_id=intent["intent_id"],
                    risk_signature=intent["risk_signature"],
                    trading_mode="live",
                    confirm_live=True,
                    user_reply="确认",
                    profile="profile",
                    language="zh",
                    pretty=False,
                )
                fake_runtime = types.SimpleNamespace(
                    risk_payload_provider=mock.Mock(
                        side_effect=RuntimeError("fresh facts changed")
                    ),
                    risk_evaluator=mock.Mock(return_value={"alerts": []}),
                    facts_provider=mock.Mock(
                        return_value={
                            "symbol": {
                                "stepSize": "0.001",
                                "minTradeAmount": "0.001",
                                "maxTradeAmount": "10",
                            }
                        }
                    ),
                )
                with mock.patch.object(weex_trade_guard, "OfficialAutoTradeRuntime", fake_runtime, create=True), mock.patch(
                    "weex_auto_trade_runtime.OfficialAutoTradeRuntime", return_value=fake_runtime
                ), mock.patch.object(weex_trade_guard, "_submit_live_auto_fallback_order") as submitter:
                    result = weex_trade_guard.cmd_confirm_order(args, now_ms=intent["created_at"] + 1)
                self.assertEqual(result, 1)
                submitter.assert_not_called()
                fake_runtime.risk_payload_provider.assert_called_once()
            finally:
                if old_home is None:
                    os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
                else:
                    os.environ["WEEX_TRADER_SKILL_HOME"] = old_home

    def test_successful_submit_with_intent_cleanup_failure_is_review_required(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            old_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
            os.environ["WEEX_TRADER_SKILL_HOME"] = tempdir
            try:
                intent = weex_order_intent_state.build_intent(
                    account_id=self.account_id,
                    market="futures",
                    trading_mode="live",
                    environment={"trading_mode": "live", "market": "futures", "uses_real_funds": True},
                    order_preview={"market": "futures", "symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "order_type": "MARKET", "quantity": 1},
                    raw_order={"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": "1"},
                    analysis_output={"alerts": [], "partial": False, "degraded_reasons": [], "constraints": []},
                    confirmation_reply_text="确认",
                    confirmation_language="zh",
                    freshness_required=False,
                )
                weex_order_intent_state.save_intent(intent)
                args = argparse.Namespace(
                    intent_id=intent["intent_id"],
                    risk_signature=intent["risk_signature"],
                    trading_mode="live",
                    confirm_live=True,
                    user_reply="确认",
                    profile="profile",
                    language="zh",
                    pretty=False,
                )
                with mock.patch.object(weex_trade_guard, "_submit_live_order", return_value={"orderId": "1"}), mock.patch.object(
                    weex_trade_guard, "clear_intent", side_effect=OSError("locked")
                ):
                    self.assertEqual(weex_trade_guard.cmd_confirm_order(args), 1)
                self.assertEqual(weex_order_intent_state.load_intent().get("submission_status"), "REVIEW_REQUIRED")
            finally:
                if old_home is None:
                    os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
                else:
                    os.environ["WEEX_TRADER_SKILL_HOME"] = old_home

    def test_partial_preview_is_not_saved_as_confirmable_intent(self) -> None:
        self.assertTrue(hasattr(weex_trade_guard, "_validate_preview_completeness"))
        with self.assertRaises(weex_trade_guard.AggregationInputError):
            weex_trade_guard._validate_preview_completeness(
                {"partial": True, "degraded_reasons": ["recent_order_history_unavailable"], "constraints": []},
                {},
            )

    def test_payload_environment_must_match_requested_mode(self) -> None:
        with self.assertRaises(weex_trade_guard.AggregationInputError):
            weex_trade_guard._environment_from_payload_or_mode(
                {"environment": {"trading_mode": "live", "market": "futures", "uses_real_funds": True}},
                "demo",
                "futures",
            )

    def test_preview_order_rejects_invalid_shape_before_aggregator(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="futures",
            trading_mode="live",
            order_json="{}",
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )
        with mock.patch.object(weex_trade_guard, "TradeDataAggregator") as aggregator:
            self.assertEqual(weex_trade_guard.cmd_preview_order(args), 1)
        aggregator.assert_not_called()

    def test_partial_preview_is_blocked_before_intent_save(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="futures",
            trading_mode="live",
            order_json='{"symbol":"BTCUSDT","side":"BUY","positionSide":"LONG","type":"MARKET","quantity":"1"}',
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )
        payload = {
            "environment": {"trading_mode": "live", "market": "futures", "uses_real_funds": True},
            "order_preview": {"market": "futures", "symbol": "BTCUSDT", "side": "BUY", "position_side": "LONG", "order_type": "MARKET", "quantity": 1},
            "product_facts": {"status": "TRADING", "minOrderSize": "0.001", "maxOrderSize": "100", "quantityPrecision": 3},
            "partial": True,
            "degraded_reasons": ["recent_order_history_unavailable"],
            "constraints": [],
        }
        fake = types.SimpleNamespace(collect_order_risk_payload=lambda **kwargs: payload)
        with mock.patch.object(weex_trade_guard, "TradeDataAggregator", return_value=fake), mock.patch.object(
            weex_trade_guard, "save_intent"
        ) as save:
            with self.assertRaises(weex_trade_guard.AggregationInputError):
                weex_trade_guard.cmd_preview_order(args)
        save.assert_not_called()

    def test_demo_conditional_order_is_rejected_before_preview(self) -> None:
        args = argparse.Namespace(
            profile="profile",
            market="futures",
            trading_mode="demo",
            order_json='{"symbol":"BTCUSDT","side":"BUY","positionSide":"LONG","type":"STOP_MARKET","quantity":"1","triggerPrice":"70000"}',
            ttl_seconds=300,
            language="zh",
            pretty=False,
        )
        with mock.patch.object(weex_trade_guard, "TradeDataAggregator") as aggregator:
            with self.assertRaisesRegex(weex_trade_guard.AggregationInputError, "DEMO_MODE_REMOVED"):
                weex_trade_guard.cmd_preview_order(args)
        aggregator.assert_not_called()

    def test_cancel_preview_and_confirm_commands_are_exposed(self) -> None:
        parser = weex_trade_guard.build_parser()
        args = parser.parse_args(["preview-cancel", "--market", "futures", "--order-id", "1", "--language", "en"])
        self.assertEqual(args.command, "preview-cancel")

    def test_cancel_confirm_uses_the_official_cancel_endpoint(self) -> None:
        class FakeApi:
            def execute_endpoint_payload(self, **kwargs):
                self.kwargs = kwargs
                return 0, {"ok": True, "result": {"orderId": "1"}}

        with tempfile.TemporaryDirectory() as tempdir:
            old_home = os.environ.get("WEEX_TRADER_SKILL_HOME")
            os.environ["WEEX_TRADER_SKILL_HOME"] = tempdir
            try:
                preview_args = argparse.Namespace(
                    profile="profile", market="futures", trading_mode="live", symbol=None,
                    order_id="1", client_oid=None, ttl_seconds=300, language="zh", pretty=False,
                )
                self.assertEqual(weex_trade_guard.cmd_preview_cancel(preview_args, now_ms=1000), 0)
                intent = weex_order_intent_state.load_intent()
                confirm_args = argparse.Namespace(
                    profile="profile", intent_id=intent["intent_id"], risk_signature=intent["risk_signature"],
                    user_reply="确认", confirm_live=False, language="zh", pretty=False,
                )
                api = FakeApi()
                with mock.patch.object(weex_trade_guard, "_build_contract_client", return_value=(api, object())):
                    self.assertEqual(weex_trade_guard.cmd_confirm_cancel(confirm_args, now_ms=1001), 1)
                self.assertFalse(hasattr(api, "kwargs"))
                confirm_args.confirm_live = True
                with mock.patch.object(weex_trade_guard, "_build_contract_client", return_value=(api, object())):
                    self.assertEqual(weex_trade_guard.cmd_confirm_cancel(confirm_args, now_ms=1001), 0)
                self.assertEqual(api.kwargs["endpoint_key"], "transaction.cancel_order")
            finally:
                if old_home is None:
                    os.environ.pop("WEEX_TRADER_SKILL_HOME", None)
                else:
                    os.environ["WEEX_TRADER_SKILL_HOME"] = old_home

    def test_submission_business_error_and_transport_uncertainty_are_distinct(self) -> None:
        class Endpoint:
            key = "transaction.place_order"

        class Client:
            def prepare_request(self, endpoint, query, body):
                return {"endpoint": endpoint.key, "body": body}

            def __init__(self, response):
                self.response = response

            def send(self, prepared):
                return self.response

        api = types.SimpleNamespace(
            ENDPOINTS={"transaction.place_order": Endpoint()},
            normalize_contract_trade_symbol=lambda value: value,
            validate_endpoint_trading_mode=lambda endpoint, mode: mode,
            generate_client_oid=lambda: "oid",
        )
        order = {"symbol": "BTCUSDT", "side": "BUY", "positionSide": "LONG", "type": "MARKET", "quantity": "1"}
        with mock.patch.object(weex_trade_guard, "_build_contract_client", return_value=(api, Client({"ok": True, "status": 200, "data": {"code": -1, "msg": "rejected"}}))):
            with self.assertRaises(weex_trade_guard.AggregationInputError):
                weex_trade_guard._submit_order(market="futures", trading_mode="live", raw_order=order, language="en")
        with mock.patch.object(weex_trade_guard, "_build_contract_client", return_value=(api, Client({"ok": False, "status": None, "error": {"message": "timeout"}}))):
            with self.assertRaises(weex_trade_guard.SubmissionUncertainError):
                weex_trade_guard._submit_order(market="futures", trading_mode="live", raw_order=order, language="en")

    def test_spot_private_mode_is_explicit_and_payload_schema_is_enforced(self) -> None:
        with self.assertRaises(SystemExit):
            weex_spot_api.normalize_trading_mode(None, required=True)
        endpoint = weex_spot_api.ENDPOINTS["spot.order.place_order"]
        client = weex_spot_api.WeexSpotClient(
            base_url="https://api-spot.weex.com",
            timeout=1,
            locale="en-US",
            api_key="k",
            api_secret="s",
            api_passphrase="p",
        )
        with self.assertRaises(SystemExit):
            client.prepare_request(endpoint, body={"unknown": "field"})
        self.assertTrue(weex_spot_api._business_error({"code": -1, "msg": "rejected"}))
        self.assertFalse(weex_spot_api._business_error({"code": "00000", "data": {}}))
        with self.assertRaises(SystemExit):
            weex_contract_api.normalize_trading_mode(None, required=True)

    def test_trade_guard_fails_closed_when_environment_credentials_are_empty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "required"):
                weex_trade_guard._build_contract_client()

    def test_empty_spot_balances_are_marked_partial(self) -> None:
        class Fetcher:
            def fetch_spot_balance(self, *, profile_name):
                return []

        reasons: list[str] = []
        balances, partial = weex_trade_guard.TradeDataAggregator(Fetcher())._collect_spot_balances(
            profile_name="p", degraded_reasons=reasons
        )
        self.assertEqual(balances, [])
        self.assertTrue(partial)
        self.assertIn("spot_balance_unavailable", reasons)

    def test_lite_does_not_expose_replay_profile_or_account_risk_cli_commands(self) -> None:
        parser = weex_trade_data_aggregator.build_parser()
        argv_by_command = {
            "collect-replay": ["collect-replay", "--profile", "p", "--market", "futures", "--period", "7d"],
            "collect-profile": ["collect-profile", "--profile", "p", "--market", "futures"],
            "collect-account-risk": ["collect-account-risk", "--profile", "p", "--market", "futures"],
            "collect-order-risk": ["collect-order-risk", "--profile", "p", "--market", "futures", "--order-json", "{}"],
        }
        for command, argv in argv_by_command.items():
            with self.subTest(command=command), self.assertRaises(SystemExit):
                parser.parse_args(argv)
        with self.assertRaises(SystemExit):
            weex_trade_guard.build_parser().parse_args(["account-scan", "--profile", "p", "--market", "futures"])


if __name__ == "__main__":
    unittest.main()
