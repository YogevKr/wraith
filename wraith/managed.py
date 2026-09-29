"""Versioned, gated orchestration for a protected browser run.

The runner performs no live action by default. ``--apply`` enables Tailnet,
exit verification, and canary commands. ``--execute`` enables the final run.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = ["MANAGED_FLOW_VERSION", "ManagedRunError", "load_config", "run_flow"]

MANAGED_FLOW_VERSION = "1"


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
    exit_check = value["exit"].get("command")
    if not _command(exit_check):
        raise ManagedRunError("Managed flow exit.command must be an argv list")
    expected = value["exit"].get("expected")
    if not isinstance(expected, str) or not expected.strip():
        raise ManagedRunError("Managed flow exit.expected is required")
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
    runner = runner or _run_command
    results: list[StepResult] = []
    tailnet = config["tailnet"]
    if tailnet.get("command"):
        result, _ = _step("tailnet", tailnet["command"], apply, runner)
        results.append(result)
    exit_result, exit_output = _step("exit", config["exit"]["command"], apply, runner)
    results.append(exit_result)
    if apply:
        expected = str(config["exit"]["expected"]).strip()
        exit_matches = exit_output.strip() == expected
        exit_output = ""
        if not exit_matches:
            raise ManagedRunError("The network exit did not match the expected home exit")
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
    step_output = str(completed.stdout or "") if name == "exit" else ""
    completed = None
    if returncode != 0:
        # Do not retain command output. It may contain a vault value.
        step_output = ""
        raise ManagedRunError(f"Managed step {name!r} failed")
    return StepResult(name, "passed"), step_output


def _run_command(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, timeout=120, **kwargs)
