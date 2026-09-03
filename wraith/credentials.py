"""Secret resolution for Wraith — values, commands, env vars, ``*_CMD`` env vars, files.

WHY THIS EXISTS
---------------
Every credential Wraith consumes (proxy-provider logins, CAPTCHA-solver API
keys) used to be resolvable only from an explicit argument, a plain
environment variable, or a plaintext ``~/.secrets`` file. That forces people
who keep secrets in a manager (1Password ``op read``, ``pass``, ``gopass``,
macOS ``security find-generic-password``, HashiCorp Vault, ...) to
*export* the value into their shell or copy it into a file first — exactly the
two things a secret manager exists to avoid.

This module lets a **command** stand in for a value everywhere a secret is
read. For a secret named ``NAME`` the resolution order is:

1. an explicit ``value`` argument;
2. an explicit ``command`` argument — run, stdout is the value;
3. the environment variable ``NAME``;
4. the environment variable ``NAME_CMD`` — a shell command whose stdout is
   the value (e.g. ``ANYIP_PASSWORD_CMD="op read op://vault/anyip/credential"``);
5. a ``NAME=value`` line in the secrets file;
6. a ``NAME_CMD=command`` line in the secrets file (a file that holds only
   *references*, never values).

The first tier that yields a non-empty value wins. Commands run through the
system shell (``/bin/sh -c`` on POSIX) with stdin closed, a timeout, and
captured output; trailing newlines are stripped from stdout and nothing else
is altered. A command that fails, times out, or prints nothing raises
:class:`SecretCommandError` — the *command line* is quoted in the message, the
secret value never is.

Used by :mod:`wraith.providers` (``DATAIMPULSE_*``, ``ANYIP_*``) and
:mod:`wraith.recaptcha` (``CAPSOLVER_API_KEY``, ``TWOCAPTCHA_API_KEY``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

__all__ = [
    "CMD_SUFFIX",
    "DEFAULT_SECRETS_FILE",
    "SecretCommandError",
    "parse_secrets_file",
    "resolve_secret",
    "run_secret_command",
]

#: Suffix that turns a secret's env-var / file key into its command form.
CMD_SUFFIX = "_CMD"
#: Default shell-style ``KEY=value`` secrets file consulted last.
DEFAULT_SECRETS_FILE = "~/.secrets"
#: Seconds a secret-producing command may run before it is killed.
DEFAULT_COMMAND_TIMEOUT = 30.0


class SecretCommandError(RuntimeError):
    """A secret-producing command failed, timed out, or printed nothing.

    The message names the command line (not a secret) so a broken
    ``*_CMD`` is diagnosable from a log; stdout is never included.
    """


# --------------------------------------------------------------------------- #
# Secrets-file parsing (shell-style KEY=value lines)
# --------------------------------------------------------------------------- #
def _strip_env_value(raw: str) -> str:
    """Normalise a value read from a ``KEY=value`` secrets-file line.

    Tolerates a surrounding pair of matching single/double quotes and trailing
    inline whitespace. (The optional ``export `` prefix is handled by the
    caller, which splits on the first ``=``.)
    """
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def parse_secrets_file(path: "Path | str", keys: "tuple[str, ...]") -> "dict[str, str]":
    """Best-effort parse of ``KEY=value`` lines from a shell-style secrets file.

    Tolerates a leading ``export `` and quoted values. Returns only the
    requested ``keys`` that are present and non-empty. Any read error (missing
    file, permissions) yields an empty mapping rather than raising —
    credential resolution must degrade gracefully.
    """
    found: dict[str, str] = {}
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found

    wanted = set(keys)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if key in wanted:
            value = _strip_env_value(value)
            if value:
                found[key] = value
    return found


# --------------------------------------------------------------------------- #
# Command execution
# --------------------------------------------------------------------------- #
def run_secret_command(
    command: str,
    *,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    name: Optional[str] = None,
) -> str:
    """Run ``command`` through the shell and return its stdout as the secret.

    * stdin is closed (a command that tries to prompt fails fast instead of
      hanging); stdout/stderr are captured, never echoed;
    * only trailing ``\\r``/``\\n`` are stripped from stdout, so a multi-line
      secret (a PEM key) survives and inner whitespace is preserved;
    * a non-zero exit, a timeout, or empty output raises
      :class:`SecretCommandError` naming the command (and the last stderr
      line) — never the output.

    Args:
        command: the shell command line, e.g.
            ``"op read op://vault/anyip/credential"`` or ``"pass show proxies/anyip"``.
        timeout: seconds before the command is killed.
        name: the secret's name, for error messages only.
    """
    label = f"{name}{CMD_SUFFIX}" if name else "secret command"
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"{label}: command is empty")
    try:
        proc = subprocess.run(  # noqa: S602 - the command is the user's own config
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SecretCommandError(
            f"{label}: command timed out after {timeout:g}s: {command!r}"
        ) from exc
    except OSError as exc:
        raise SecretCommandError(f"{label}: command could not start ({exc}): {command!r}") from exc

    if proc.returncode != 0:
        err_lines = (proc.stderr or "").strip().splitlines()
        tail = f" ({err_lines[-1].strip()})" if err_lines else ""
        raise SecretCommandError(
            f"{label}: command exited {proc.returncode}{tail}: {command!r}"
        )
    value = (proc.stdout or "").rstrip("\r\n")
    if not value.strip():
        raise SecretCommandError(f"{label}: command printed nothing: {command!r}")
    return value


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def resolve_secret(
    name: str,
    *,
    value: Optional[str] = None,
    command: Optional[str] = None,
    secrets_file: "Optional[str | Path]" = DEFAULT_SECRETS_FILE,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
) -> Optional[str]:
    """Resolve the secret ``name`` through every tier; ``None`` if nothing has it.

    Order (first non-empty wins): explicit ``value`` → explicit ``command`` →
    env ``NAME`` → env ``NAME_CMD`` → ``secrets_file`` ``NAME=`` →
    ``secrets_file`` ``NAME_CMD=``. Pass ``secrets_file=None`` to skip the file.

    Values are returned as found (no stripping) except command output, which
    loses its trailing newlines. A failing command raises
    :class:`SecretCommandError` rather than falling through, so a broken
    ``*_CMD`` is loud instead of silently degrading to a lower tier.
    """
    if not name or not isinstance(name, str):
        raise ValueError("secret name must be a non-empty str")

    if isinstance(value, str) and value != "":
        return value
    if isinstance(command, str) and command.strip():
        return run_secret_command(command, timeout=timeout, name=name)

    cmd_key = name + CMD_SUFFIX
    env_value = os.environ.get(name)
    if env_value:
        return env_value
    env_cmd = os.environ.get(cmd_key)
    if env_cmd and env_cmd.strip():
        return run_secret_command(env_cmd, timeout=timeout, name=name)

    if secrets_file:
        parsed = parse_secrets_file(secrets_file, (name, cmd_key))
        file_value = parsed.get(name)
        if file_value:
            return file_value
        file_cmd = parsed.get(cmd_key)
        if file_cmd:
            return run_secret_command(file_cmd, timeout=timeout, name=name)

    return None
