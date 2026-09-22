#!/usr/bin/env python3
"""Lightweight AI-facing state cache for WEEX skill entrypoints."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from weex_url_policy import BaseUrlPolicyError, validate_weex_base_url


CONFIG_HOME_ENV = "WEEX_TRADER_SKILL_HOME"
AGENT_INIT_FILENAME = "agent-init.json"
AGENT_RUNTIME_FILENAME = "agent-runtime.json"
REQUIRED_MODULES = ("requests",)
RUNTIME_ENV_VARS = (
    "WEEX_API_KEY",
    "WEEX_API_SECRET",
    "WEEX_API_PASSPHRASE",
    "WEEX_TRADER_SKILL_HOME",
    "WEEX_LOCALE",
    "WEEX_API_TIMEOUT",
    "WEEX_API_BASE",
    "WEEX_CONTRACT_API_BASE",
    "WEEX_SPOT_API_BASE",
)
BASE_URL_ENV_VARS = (
    "WEEX_API_BASE",
    "WEEX_CONTRACT_API_BASE",
    "WEEX_SPOT_API_BASE",
)


class RuntimePreflightError(RuntimeError):
    """Raised when the current runtime cannot safely execute private WEEX commands."""

    def __init__(
        self,
        message: str,
        *,
        missing_modules: Optional[list[str]] = None,
        env_issues: Optional[list[str]] = None,
        setup_result: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.missing_modules = tuple(missing_modules or ())
        self.env_issues = tuple(env_issues or ())
        self.setup_result = setup_result


def config_dir() -> Path:
    raw = os.getenv(CONFIG_HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".weex-trader-skill"


def agent_init_path() -> Path:
    return config_dir() / AGENT_INIT_FILENAME


def agent_runtime_path() -> Path:
    return config_dir() / AGENT_RUNTIME_FILENAME


def requirements_path() -> Path:
    return Path(__file__).resolve().parent.parent / "requirements.txt"


def requirements_lock_path() -> Path:
    return Path(__file__).resolve().parent.parent / "requirements.lock"


def runtime_setup_script_path() -> Path:
    return Path(__file__).resolve().parent / "weex_runtime_setup.py"


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_config_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_config_dir(path.parent)
    temp_path: Optional[Path] = None
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _launcher_for_os(os_family: str) -> str:
    return "py -3" if os_family == "Windows" else "python3"


def _probe_required_modules() -> tuple[bool, list[str]]:
    missing: list[str] = []
    for module_name in REQUIRED_MODULES:
        try:
            importlib.import_module(module_name)
        except Exception:
            missing.append(module_name)
    return not missing, missing


def validate_runtime_environment(env: Optional[dict[str, str]] = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    issues: list[str] = []

    credential_names = ("WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE")
    credential_presence = {
        name: bool(_clean_text(source.get(name))) for name in credential_names
    }
    if any(credential_presence.values()) and not all(credential_presence.values()):
        missing = [name for name, present in credential_presence.items() if not present]
        issues.append(
            "WEEX_API_KEY, WEEX_API_SECRET, and WEEX_API_PASSPHRASE must be provided "
            "together. Missing: " + ", ".join(missing)
        )

    raw_timeout = _clean_text(source.get("WEEX_API_TIMEOUT"))
    if raw_timeout:
        try:
            timeout_value = float(raw_timeout)
        except ValueError:
            issues.append(
                f"WEEX_API_TIMEOUT must be a positive number of seconds; got {raw_timeout!r}."
            )
        else:
            if not math.isfinite(timeout_value) or timeout_value <= 0:
                issues.append(
                    f"WEEX_API_TIMEOUT must be a positive finite number of seconds; got {raw_timeout!r}."
                )

    for env_name in BASE_URL_ENV_VARS:
        raw_url = _clean_text(source.get(env_name))
        if not raw_url:
            continue
        try:
            validate_weex_base_url(raw_url, label=env_name)
        except BaseUrlPolicyError as exc:
            issues.append(str(exc))

    return {
        "ok": not issues,
        "issues": issues,
    }


def _dependency_install_command(os_family: Optional[str] = None) -> str:
    launcher = _launcher_for_os(os_family or platform.system())
    return f"{launcher} -m pip install --require-hashes -r {requirements_lock_path()}"


def _run_runtime_setup() -> dict[str, Any]:
    command = [sys.executable, str(runtime_setup_script_path())]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = completed.stdout.strip()
    payload = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": stdout,
        "stderr": completed.stderr.strip(),
        "payload": payload,
    }


def _clear_runtime_sensitive_module_cache() -> None:
    return None


def _raise_private_runtime_preflight_error(
    *,
    command: Optional[str],
    missing_modules: list[str],
    env_validation: dict[str, Any],
    setup_result: Optional[dict[str, Any]] = None,
) -> None:
    lines = ["Private WEEX command preflight failed."]
    if command:
        lines.append(f"Command: {command}")
    if setup_result is not None:
        lines.append(
            f"Automatic runtime setup was attempted with: {' '.join(setup_result['command'])}"
        )
        payload = setup_result.get("payload")
        if setup_result["returncode"] != 0:
            lines.append("Automatic runtime setup did not complete successfully.")
            if setup_result.get("stderr"):
                lines.append(f"Runtime setup stderr: {setup_result['stderr']}")
            elif setup_result.get("stdout"):
                lines.append(f"Runtime setup output: {setup_result['stdout']}")
        elif isinstance(payload, dict) and payload.get("ok"):
            lines.append(
                "Automatic runtime setup completed, but this process is still missing runtime prerequisites."
            )
            lines.append("Retry the same private command in a fresh shell if the issue persists.")
        else:
            lines.append("Automatic runtime setup completed, but the interpreter is still not ready.")
    if missing_modules:
        modules = ", ".join(missing_modules)
        lines.append(f"Missing Python dependencies for this interpreter: {modules}.")
        lines.append(f"Install them with: {_dependency_install_command()}")
    env_issues = list(env_validation["issues"])
    if env_issues:
        lines.append("Invalid runtime environment:")
        lines.extend(f"- {issue}" for issue in env_issues)

    raise RuntimePreflightError(
        "\n".join(lines),
        missing_modules=missing_modules,
        env_issues=env_issues,
        setup_result=setup_result,
    )


def ensure_private_runtime_ready(
    command: Optional[str] = None,
    *,
    auto_setup: bool = False,
) -> None:
    requirements_ready, missing_modules = _probe_required_modules()
    env_validation = validate_runtime_environment()
    if requirements_ready and env_validation["ok"]:
        return

    setup_result: Optional[dict[str, Any]] = None
    if auto_setup and missing_modules and env_validation["ok"]:
        setup_result = _run_runtime_setup()
        importlib.invalidate_caches()
        _clear_runtime_sensitive_module_cache()
        requirements_ready, missing_modules = _probe_required_modules()
        env_validation = validate_runtime_environment()
        if requirements_ready and env_validation["ok"]:
            return

    _raise_private_runtime_preflight_error(
        command=command,
        missing_modules=missing_modules,
        env_validation=env_validation,
        setup_result=setup_result,
    )


def build_agent_init_state() -> dict[str, Any]:
    os_family = platform.system()
    credential_presence = {
        name: bool(_clean_text(os.getenv(name)))
        for name in ("WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE")
    }

    return {
        "schema_version": 1,
        "last_refreshed_at": _now_iso(),
        "host": {
            "os_family": os_family,
            "os_release": platform.release(),
            "launcher": _launcher_for_os(os_family),
            "python_executable": sys.executable,
            "config_dir": str(config_dir()),
        },
        "routes": {
            "public_api_launcher": _launcher_for_os(os_family),
            "private_api_requires": [
                "direct_contract_spot:complete_environment_credentials",
                "trade_guard:complete_environment_credentials",
                "automated_authorization:complete_environment_credentials",
            ],
        },
        "credentials": {
            "source": "environment",
            "complete": all(credential_presence.values()),
            "present": credential_presence,
        },
    }


def build_agent_runtime_state(
    command: Optional[str] = None,
) -> dict[str, Any]:
    os_family = platform.system()
    requirements_ready, missing_modules = _probe_required_modules()
    env_validation = validate_runtime_environment()
    credential_presence = {
        name: bool(_clean_text(os.getenv(name)))
        for name in ("WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE")
    }

    return {
        "schema_version": 1,
        "last_verified_at": _now_iso(),
        "command": command,
        "host": {
            "os_family": os_family,
            "launcher": _launcher_for_os(os_family),
            "python_executable": sys.executable,
            "requirements_ready": requirements_ready,
            "missing_modules": missing_modules,
        },
        "env": {
            env_name: bool(_clean_text(os.getenv(env_name)))
            for env_name in RUNTIME_ENV_VARS
        },
        "env_validation": env_validation,
        "credentials": {
            "source": "environment",
            "complete": all(credential_presence.values()),
            "present": credential_presence,
        },
    }


def refresh_agent_init_state() -> dict[str, Any]:
    payload = build_agent_init_state()
    _atomic_write_json(agent_init_path(), payload)
    return payload


def refresh_agent_runtime_state(
    command: Optional[str] = None,
) -> dict[str, Any]:
    payload = build_agent_runtime_state(command=command)
    _atomic_write_json(agent_runtime_path(), payload)
    return payload


def refresh_agent_records(
    command: Optional[str] = None,
) -> dict[str, dict[str, Any]]:
    return {
        "init": refresh_agent_init_state(),
        "runtime": refresh_agent_runtime_state(command=command),
    }


def _output_json(payload: dict[str, Any], pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh or inspect non-secret AI cache files for the WEEX trader skill.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--command",
        default="agent-state.refresh",
        help="Command label to store in agent-runtime.json",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    payload = refresh_agent_records(command=args.command)
    _output_json(payload, args.pretty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
