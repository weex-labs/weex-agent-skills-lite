#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import weex_agent_state as agent_state  # noqa: E402
import weex_language  # noqa: E402


class AgentStateEnvironmentOnlyTests(unittest.TestCase):
    def test_init_routes_every_private_flow_to_complete_environment_credentials(self) -> None:
        payload = agent_state.build_agent_init_state()

        self.assertNotIn("profiles", payload)
        self.assertNotIn("vault", payload)
        self.assertEqual(
            payload["routes"]["private_api_requires"],
            [
                "direct_contract_spot:complete_environment_credentials",
                "trade_guard:complete_environment_credentials",
                "automated_authorization:complete_environment_credentials",
            ],
        )
        self.assertEqual(payload["credentials"]["source"], "environment")

    def test_agent_state_does_not_persist_language_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with mock.patch.dict(
                os.environ,
                {"WEEX_TRADER_SKILL_HOME": tempdir},
                clear=True,
            ):
                records = agent_state.refresh_agent_records(command="skill.preflight")

                self.assertNotIn("language", records["init"])
                self.assertNotIn("language", records["runtime"])
                self.assertNotIn("language", agent_state.agent_init_path().read_text())
                self.assertNotIn("language", agent_state.agent_runtime_path().read_text())

    def test_language_resolution_requires_an_explicit_supported_language(self) -> None:
        with self.assertRaises(weex_language.LanguageRequiredError):
            weex_language.resolve_language(None)
        self.assertEqual(weex_language.resolve_language("fr"), "fr")
        self.assertEqual(weex_language.resolve_language("ja"), "ja")
        self.assertEqual(weex_language.resolve_language("en_us"), "en-US")
        self.assertEqual(weex_language.resolve_language("pt_br"), "pt-BR")
        self.assertEqual(weex_language.resolve_language("zh"), "zh-CN")
        self.assertEqual(weex_language.resolve_language("en"), "en-US")

    def test_language_context_preserves_invocation_source_without_persistence(self) -> None:
        context = weex_language.resolve_language_context("en", source="fallback")
        self.assertEqual(context.language, "en-US")
        self.assertEqual(context.source, "fallback")

    def test_detected_unsupported_language_falls_back_to_english(self) -> None:
        decision = weex_language.resolve_language_decision("xx")
        self.assertEqual(decision.input_language, "xx")
        self.assertEqual(decision.render_language, "en-US")
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.fallback_reason, "unsupported_language")

        unknown = weex_language.resolve_language_decision(None)
        self.assertIsNone(unknown.input_language)
        self.assertEqual(unknown.render_language, "en-US")
        self.assertEqual(unknown.source, "fallback")
        self.assertEqual(unknown.fallback_reason, "language_undetermined")

    def test_openclaw_japanese_order_request_never_selects_chinese_templates(self) -> None:
        decision = weex_language.resolve_language_decision(
            "ja",
            confidence=0.99,
        )
        self.assertEqual(decision.render_language, "ja")
        self.assertNotEqual(decision.render_language, "zh-CN")
        self.assertIsNone(decision.fallback_reason)

    def test_low_confidence_language_detection_falls_back_to_english(self) -> None:
        decision = weex_language.resolve_language_decision("zh", confidence=0.4)
        self.assertEqual(decision.render_language, "en-US")
        self.assertEqual(decision.source, "fallback")
        self.assertEqual(decision.fallback_reason, "low_confidence")

    def test_detected_language_cannot_be_rendered_as_a_different_supported_language(self) -> None:
        with self.assertRaises(weex_language.LanguageMismatchError):
            weex_language.resolve_language_decision("ja", render_language="zh")
        with self.assertRaises(weex_language.LanguageMismatchError):
            weex_language.resolve_language_decision("zh", render_language="en")
        with self.assertRaises(weex_language.LanguageMismatchError):
            weex_language.resolve_language_decision(None, render_language="zh")

    def test_preflight_is_language_neutral_without_persisting_a_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, mock.patch.dict(
            os.environ,
            {"WEEX_TRADER_SKILL_HOME": tempdir},
            clear=True,
        ):
            self.assertEqual(agent_state.main(["--command", "skill.preflight", "--pretty"]), 0)

    def test_runtime_reports_presence_without_exposing_values(self) -> None:
        credentials = {
            "WEEX_API_KEY": "env-api-key",
            "WEEX_API_SECRET": "env-api-secret",
            "WEEX_API_PASSPHRASE": "env-api-passphrase",
        }
        with mock.patch.dict(os.environ, credentials, clear=True):
            payload = agent_state.build_agent_runtime_state()

        self.assertTrue(payload["credentials"]["complete"])
        self.assertEqual(
            payload["credentials"]["present"],
            {name: True for name in credentials},
        )
        serialized = json.dumps(payload)
        for value in credentials.values():
            self.assertNotIn(value, serialized)

    def test_blank_or_partial_environment_credentials_are_not_complete(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "WEEX_API_KEY": " ",
                "WEEX_API_SECRET": "present",
                "WEEX_API_PASSPHRASE": "\t",
            },
            clear=True,
        ):
            payload = agent_state.build_agent_runtime_state()

        self.assertFalse(payload["credentials"]["complete"])
        self.assertEqual(
            payload["credentials"]["present"],
            {
                "WEEX_API_KEY": False,
                "WEEX_API_SECRET": True,
                "WEEX_API_PASSPHRASE": False,
            },
        )

    def test_runtime_environment_validation_rejects_partial_credentials_and_invalid_overrides(self) -> None:
        partial = agent_state.validate_runtime_environment({"WEEX_API_KEY": "only-key"})
        self.assertFalse(partial["ok"])
        self.assertTrue(any("provided together" in item for item in partial["issues"]))

        invalid = agent_state.validate_runtime_environment(
            {
                "WEEX_API_TIMEOUT": "0",
                "WEEX_SPOT_API_BASE": "http://example.com",
            }
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(len(invalid["issues"]), 2)

    def test_refresh_writes_owner_only_non_secret_cache_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            env = {
                "WEEX_TRADER_SKILL_HOME": tempdir,
                "WEEX_API_KEY": "key",
                "WEEX_API_SECRET": "secret",
                "WEEX_API_PASSPHRASE": "passphrase",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                records = agent_state.refresh_agent_records(
                    command="test.preflight",
                )
                init_path = agent_state.agent_init_path()
                runtime_path = agent_state.agent_runtime_path()

            self.assertEqual(records["runtime"]["command"], "test.preflight")
            self.assertEqual(stat.S_IMODE(init_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(runtime_path.stat().st_mode), 0o600)
            combined = init_path.read_text() + runtime_path.read_text()
            self.assertNotIn("secret", combined)
            self.assertNotIn("passphrase", combined)

    def test_private_runtime_preflight_preserves_dependency_and_environment_fail_closed_checks(self) -> None:
        with mock.patch.object(agent_state, "_probe_required_modules", return_value=(False, ["requests"])):
            with mock.patch.object(
                agent_state,
                "validate_runtime_environment",
                return_value={"ok": False, "issues": ["bad environment"]},
            ):
                with self.assertRaises(agent_state.RuntimePreflightError) as raised:
                    agent_state.ensure_private_runtime_ready(command="private.test")

        self.assertEqual(raised.exception.missing_modules, ("requests",))
        self.assertEqual(raised.exception.env_issues, ("bad environment",))


if __name__ == "__main__":
    unittest.main()
