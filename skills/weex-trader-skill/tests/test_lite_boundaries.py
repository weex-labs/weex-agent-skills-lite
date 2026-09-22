from __future__ import annotations

import json
import inspect
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class LiteBoundaryTests(unittest.TestCase):
    def test_runtime_trading_surface_is_live_only(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import weex_contract_api  # type: ignore
        import weex_spot_api  # type: ignore
        import weex_trade_data_aggregator  # type: ignore
        import weex_trade_guard  # type: ignore

        self.assertEqual(weex_contract_api.TRADING_MODES, ("live",))
        self.assertEqual(weex_spot_api.TRADING_MODES, ("live",))
        self.assertEqual(weex_trade_data_aggregator.TRADING_MODES, ("live",))
        self.assertEqual(weex_trade_guard.TRADING_MODES, ("live",))

        for normalize in (
            weex_contract_api.normalize_trading_mode,
            weex_spot_api.normalize_trading_mode,
            weex_trade_data_aggregator._normalize_trading_mode,
            weex_trade_guard._normalize_trading_mode,
        ):
            with self.subTest(normalize=normalize):
                with self.assertRaisesRegex((SystemExit, ValueError), "DEMO_MODE_REMOVED"):
                    normalize("demo")

    def test_contract_runtime_registry_excludes_simulated_endpoints(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import weex_contract_api  # type: ignore

        self.assertFalse(any(key.startswith("sim.") for key in weex_contract_api.ENDPOINTS))
        self.assertNotIn("confirm_demo", inspect.signature(weex_contract_api.execute_endpoint_payload).parameters)
        self.assertNotIn("--confirm-demo", weex_contract_api.build_parser().format_help())

    def test_low_level_environment_payloads_are_structured_not_presentational(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import weex_contract_api  # type: ignore
        import weex_spot_api  # type: ignore

        self.assertNotIn("notice", weex_contract_api.environment_for_mode("live"))
        self.assertNotIn("notice", weex_spot_api.private_environment())

    def test_project_contains_only_the_trader_skill(self) -> None:
        skill_dirs = sorted(
            path.name
            for path in (ROOT.parent).iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        )
        self.assertEqual(skill_dirs, ["weex-trader-skill"])

    def test_manifest_is_openclaw_only_and_keeps_auto_authorization(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["host_support"]["openclaw"]["supported"])
        self.assertFalse(manifest["host_support"]["other_hosts"]["supported"])
        self.assertEqual(manifest["host_support"]["openclaw"]["linked_skills"], ["weex-trader-skill"])
        self.assertIn("submit-auto", manifest["routing"]["automated_strategy_authorization"]["commands"])
        self.assertEqual(manifest["credential_policy"]["source"], "runtime_environment_only")
        self.assertEqual(
            manifest["credential_policy"]["required_together"],
            ["WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE"],
        )
        self.assertTrue(
            {
                "saved profiles and vaults",
                "account risk reports",
                "monitor",
                "partner",
                "profile analysis",
            }.issubset(set(manifest["excluded_capabilities"]))
        )

    def test_manifest_lists_every_auto_trade_facade_command(self) -> None:
        import sys

        sys.path.insert(0, str(SCRIPTS))
        import weex_auto_trade  # type: ignore

        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        declared = set(manifest["routing"]["automated_strategy_authorization"]["commands"])
        self.assertEqual(declared, set(weex_auto_trade.COMMAND_SCHEMAS))

    def test_manifest_keeps_explicit_spot_ticker_entrypoint(self) -> None:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("ticker", manifest["routing"]["spot"]["commands"])

    def test_required_market_account_and_trade_endpoints_remain_catalogued(self) -> None:
        contract = json.loads((ROOT / "references" / "contract-api-definitions.json").read_text(encoding="utf-8"))
        spot = json.loads((ROOT / "references" / "spot-api-definitions.json").read_text(encoding="utf-8"))
        contract_keys = {item["key"] for item in contract["definitions"]}
        spot_keys = {item["key"] for item in spot["definitions"]}
        self.assertTrue(
            {
                "market.get_symbol_price",
                "market.get_history_klines",
                "market.get_depth_data",
                "market.get_current_funding_rate",
                "market.get_funding_rate_history",
                "account.get_account_balance",
                "account.get_all_positions",
                "transaction.get_order_history",
                "transaction.get_trade_details",
                "transaction.place_order",
                "transaction.cancel_order",
                "transaction.place_pending_order",
                "transaction.place_tp_sl_order",
            }.issubset(contract_keys)
        )
        self.assertTrue(
            {
                "spot.market.get_ticker_info",
                "spot.market.get_k_line_data",
                "spot.market.get_depth_data",
                "spot.account.get_account_balance",
                "spot.order.history_orders",
                "spot.order.transaction_details",
                "spot.order.place_order",
                "spot.order.cancel_order",
                "spot.order.order_details",
            }.issubset(spot_keys)
        )

    def test_spot_registry_excludes_partner_rebate_endpoints(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import weex_spot_api  # type: ignore

        self.assertTrue(weex_spot_api.ENDPOINTS)
        self.assertFalse(any(key.startswith("spot.rebate.") for key in weex_spot_api.ENDPOINTS))

    def test_openclaw_updater_links_one_skill(self) -> None:
        updater = (SCRIPTS / "update_openclaw_skills.sh").read_text(encoding="utf-8")
        self.assertIn('WEEX_SKILLS=("weex-trader-skill")', updater)
        self.assertNotIn("weex-analysis-skill", updater)
        self.assertNotIn("weex-monitor-skill", updater)
        self.assertNotIn("weex-partner-skill", updater)

    def test_runtime_routes_do_not_advertise_partner(self) -> None:
        state = (SCRIPTS / "weex_agent_state.py").read_text(encoding="utf-8")
        self.assertNotIn("partner_aggregation", state)

    def test_aggregator_has_no_replay_or_profile_collection_surface(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        import weex_trade_data_aggregator  # type: ignore

        self.assertFalse(hasattr(weex_trade_data_aggregator.TradeDataAggregator, "collect_replay_payload"))
        self.assertFalse(hasattr(weex_trade_data_aggregator.TradeDataAggregator, "collect_profile_payload"))
        self.assertFalse(hasattr(weex_trade_data_aggregator.WeexApiFetcher, "fetch_futures_fills"))
        self.assertFalse(hasattr(weex_trade_data_aggregator.WeexApiFetcher, "fetch_spot_fills"))

    def test_trader_skill_is_the_only_implementation_layer(self) -> None:
        self.assertFalse((ROOT.parent / "_shared" / "weex_risk_review_core.py").exists())

    def test_file_index_covers_all_skill_scripts_and_references(self) -> None:
        index = json.loads((ROOT / "file-index.json").read_text(encoding="utf-8"))
        indexed = set(index["file_guide"])
        actual = {
            f"{directory.name}/{path.name}"
            for directory in (ROOT / "scripts", ROOT / "references")
            for path in directory.iterdir()
            if path.is_file()
        }
        self.assertEqual(actual - indexed, set())

    def test_core_cli_help_is_available(self) -> None:
        for script in (
            "weex_contract_api.py",
            "weex_spot_api.py",
            "weex_trade_guard.py",
            "weex_auto_trade.py",
        ):
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--help"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, f"{script}: {completed.stderr}")


if __name__ == "__main__":
    unittest.main()
