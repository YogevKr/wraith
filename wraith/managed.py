"""Versioned, gated orchestration for a protected browser run.

The runner performs no live action by default. ``--apply`` enables Tailnet,
exit IP and identity verification, and canary commands. ``--execute`` enables
the final run.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = ["MANAGED_FLOW_VERSION", "ManagedRunError", "load_config", "run_flow"]

MANAGED_FLOW_VERSION = "2"


class ManagedRunError(RuntimeError):
    """The managed flow configuration or a required step failed."""


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str


def load_config(path: str | Path) -> dict[str, Any]:
    """Load and validate a JSON flow configuration."""
    config_path = Path(path).expanduser()
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = None
    if value is None:
        raise ManagedRunError("Could not read the managed flow configuration")
    if not isinstance(value, dict) or str(value.get("version", "")) != MANAGED_FLOW_VERSION:
        raise ManagedRunError("Unsupported managed flow configuration version")
    for section in ("tailnet", "exit", "canary", "run"):
        if not isinstance(value.get(section), dict):
            raise ManagedRunError(f"Managed flow section {section!r} is required")
    for section in ("tailnet", "run"):
        command = value[section].get("command")
        if command is not None and not _command(command):
            raise ManagedRunError(f"Managed flow command {section!r} must be an argv list")
    if not _command(value["canary"].get("command")):
        raise ManagedRunError("Managed flow canary.command is required")
    _validated_exit_config(value["exit"])
    return value


def run_flow(
    config: dict[str, Any],
    *,
    apply: bool = False,
    execute: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[StepResult]:
    """Run the gated flow and return status-only results.

    Commands use argv arrays. Their stdout and stderr never reach the caller.
    """
    if execute and not apply:
        raise ManagedRunError("--execute requires --apply")
    if execute and not _command(config.get("canary", {}).get("command")):
        raise ManagedRunError("--execute requires a vault canary command")
    exit_command, expected, identity_command, identity_expected = _validated_exit_config(
        config.get("exit")
    )
    runner = runner or _run_command
    results: list[StepResult] = []
    tailnet = config["tailnet"]
    if tailnet.get("command"):
        result, _ = _step("tailnet", tailnet["command"], apply, runner)
        results.append(result)
    exit_result, exit_output = _step("exit", exit_command, apply, runner)
    results.append(exit_result)
    if apply:
        exit_matches = exit_output.strip() == expected
        exit_output = ""
        if not exit_matches:
            raise ManagedRunError("The network exit did not match the expected home exit")
    identity_result, identity_output = _step("exit_identity", identity_command, apply, runner)
    results.append(identity_result)
    if apply:
        identity_matches = _identity_matches(identity_output, identity_expected)
        identity_output = ""
        if not identity_matches:
            raise ManagedRunError("The active exit node identity or health could not be verified")
    canary = config["canary"].get("command")
    if canary:
        result, _ = _step("canary", canary, apply, runner)
        results.append(result)
    command = config["run"].get("command")
    if command:
        result, _ = _step("run", command, execute, runner)
        results.append(result)
    return results


def _command(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item for item in value
    )


def _validated_exit_config(
    value: Any,
) -> tuple[Sequence[str], str, Sequence[str], str]:
    """Validate public-IP and Tailscale identity checks for a managed flow."""
    if not isinstance(value, dict):
        raise ManagedRunError("Managed flow exit section is required")
    exit_command = value.get("command")
    if not _command(exit_command):
        raise ManagedRunError("Managed flow exit.command must be an argv list")
    expected = value.get("expected")
    if not isinstance(expected, str) or not expected.strip():
        raise ManagedRunError("Managed flow exit.expected is required")
    identity_command = value.get("identity_command")
    if not _command(identity_command):
        raise ManagedRunError("Managed flow exit.identity_command is required")
    identity_expected = value.get("identity_expected")
    if not isinstance(identity_expected, str) or not identity_expected.strip():
        raise ManagedRunError("Managed flow exit.identity_expected is required")
    return exit_command, expected.strip(), identity_command, identity_expected.strip()


def _step(
    name: str,
    command: Sequence[str],
    enabled: bool,
    runner: Callable[..., Any],
) -> tuple[StepResult, str]:
    if not enabled:
        return StepResult(name, "planned"), ""
    completed = None
    try:
        completed = runner(command, capture_output=True, text=True, check=False)
    except Exception:
        pass
    # Raise outside the handler: even __context__ must not retain process output.
    if completed is None:
        raise ManagedRunError(f"Managed step {name!r} could not start")
    returncode = completed.returncode
    step_output = str(completed.stdout or "") if name in {"exit", "exit_identity"} else ""
    completed = None
    if returncode != 0:
        # Do not retain command output. It may contain a vault value.
        step_output = ""
        raise ManagedRunError(f"Managed step {name!r} failed")
    return StepResult(name, "passed"), step_output


def _run_command(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, timeout=120, **kwargs)


def _identity_matches(output: str, expected: str) -> bool:
    """Verify a healthy, active Tailscale exit node by control-plane identity."""
    try:
        status = json.loads(output)
    except (TypeError, ValueError):
        return False
    if not isinstance(status, dict):
        return False
    if str(status.get("BackendState", "")).strip().lower() != "running":
        return False
    exit_node = status.get("ExitNodeStatus")
    if not isinstance(exit_node, dict) or exit_node.get("Online") is not True:
        return False
    expected_value = expected.strip()
    values: set[str] = set()
    for key in ("ID", "HostName", "DNSName"):
        value = exit_node.get(key)
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    addresses = exit_node.get("TailscaleIPs")
    if isinstance(addresses, list):
        values.update(value.strip() for value in addresses if isinstance(value, str) and value.strip())
    return expected_value in values
