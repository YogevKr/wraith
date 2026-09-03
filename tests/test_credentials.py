"""Offline tests for wraith.credentials — command-backed secret resolution.

Covers the resolution order (value → command → env → env ``*_CMD`` → file →
file ``*_CMD``), command execution semantics (trailing-newline strip only,
failure / empty / timeout → ``SecretCommandError`` that never quotes stdout),
and the integration points: both proxy providers (``*_cmd`` kwargs, env
``*_CMD``, file ``*_CMD``), the CAPTCHA solver adapters (``api_key_cmd`` /
``CAPSOLVER_API_KEY`` / ``TWOCAPTCHA_API_KEY``), and the CLI
``--proxy-username-cmd`` / ``--proxy-password-cmd`` flags.

Commands are tiny POSIX shell one-liners (``echo``, ``exit``, ``sleep``) — no
network, no secret manager.
"""

from __future__ import annotations

import argparse

import pytest

from wraith import cli
from wraith.credentials import (
    SecretCommandError,
    parse_secrets_file,
    resolve_secret,
    run_secret_command,
)
from wraith.providers import AnyIP, DataImpulse
from wraith.recaptcha import CapSolver, SolverService, TwoCaptcha

NAME = "WRAITH_TEST_SECRET"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """No ambient values: clear every var this file touches and point HOME at tmp."""
    for k in (
        NAME, NAME + "_CMD",
        "ANYIP_USERNAME", "ANYIP_PASSWORD", "ANYIP_USERNAME_CMD", "ANYIP_PASSWORD_CMD",
        "DATAIMPULSE_USERNAME", "DATAIMPULSE_PASSWORD",
        "DATAIMPULSE_USERNAME_CMD", "DATAIMPULSE_PASSWORD_CMD",
        "CAPSOLVER_API_KEY", "CAPSOLVER_API_KEY_CMD",
        "TWOCAPTCHA_API_KEY", "TWOCAPTCHA_API_KEY_CMD",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # ~/.secrets -> empty tmp home


# --------------------------------------------------------------------------- #
# run_secret_command
# --------------------------------------------------------------------------- #

def test_run_command_strips_only_trailing_newlines():
    assert run_secret_command("echo hunter2") == "hunter2"
    assert run_secret_command("printf '  spaced  \\n\\n'") == "  spaced  "
    assert run_secret_command("printf 'line1\\nline2\\n'") == "line1\nline2"


def test_run_command_failure_names_command_not_output(monkeypatch):
    # The value comes from the environment, not the command text, so the only
    # way it could reach the message is via captured stdout — which must not.
    monkeypatch.setenv("WRAITH_LEAK_PROBE", "LEAKED-VALUE")
    with pytest.raises(SecretCommandError) as ei:
        run_secret_command('printf %s "$WRAITH_LEAK_PROBE"; echo boom >&2; exit 3', name="X")
    msg = str(ei.value)
    assert "exited 3" in msg and "boom" in msg and "X_CMD" in msg
    assert "LEAKED-VALUE" not in msg


def test_run_command_empty_output_is_an_error():
    with pytest.raises(SecretCommandError, match="printed nothing"):
        run_secret_command("true")
    with pytest.raises(SecretCommandError, match="printed nothing"):
        run_secret_command("printf '\\n'")


def test_run_command_timeout():
    with pytest.raises(SecretCommandError, match="timed out"):
        run_secret_command("sleep 5", timeout=0.2)


def test_run_command_rejects_blank():
    with pytest.raises(ValueError):
        run_secret_command("   ")


def test_run_command_stdin_is_closed():
    # `cat` with an open stdin would hang; with DEVNULL it returns immediately
    # and prints nothing -> "printed nothing", not a timeout.
    with pytest.raises(SecretCommandError, match="printed nothing"):
        run_secret_command("cat", timeout=5)


# --------------------------------------------------------------------------- #
# resolve_secret — order of tiers
# --------------------------------------------------------------------------- #

def test_value_wins_and_command_does_not_run():
    # a failing command proves it was never executed
    assert resolve_secret(NAME, value="v", command="exit 1", secrets_file=None) == "v"


def test_empty_value_falls_through_to_command():
    assert resolve_secret(NAME, value="", command="echo c", secrets_file=None) == "c"


def test_command_beats_env(monkeypatch):
    monkeypatch.setenv(NAME, "envval")
    assert resolve_secret(NAME, command="echo c", secrets_file=None) == "c"


def test_env_value_beats_env_cmd(monkeypatch):
    monkeypatch.setenv(NAME, "envval")
    monkeypatch.setenv(NAME + "_CMD", "exit 1")
    assert resolve_secret(NAME, secrets_file=None) == "envval"


def test_env_cmd_runs(monkeypatch):
    monkeypatch.setenv(NAME + "_CMD", "echo from-cmd")
    assert resolve_secret(NAME, secrets_file=None) == "from-cmd"


def test_env_cmd_failure_is_loud_not_silent(monkeypatch, tmp_path):
    monkeypatch.setenv(NAME + "_CMD", "exit 2")
    f = tmp_path / "s"
    f.write_text(f"{NAME}=filefallback\n")
    with pytest.raises(SecretCommandError):
        resolve_secret(NAME, secrets_file=f)


def test_file_value_then_file_cmd(tmp_path):
    f = tmp_path / "s"
    f.write_text(f"export {NAME}_CMD='echo filecmd'\n")
    assert resolve_secret(NAME, secrets_file=f) == "filecmd"
    f.write_text(f'{NAME}="fileval"\n{NAME}_CMD=exit 1\n')
    assert resolve_secret(NAME, secrets_file=f) == "fileval"


def test_secrets_file_none_skips_file(tmp_path):
    f = tmp_path / "s"
    f.write_text(f"{NAME}=fileval\n")
    assert resolve_secret(NAME, secrets_file=f) == "fileval"
    assert resolve_secret(NAME, secrets_file=None) is None


def test_nothing_gives_none():
    assert resolve_secret(NAME, secrets_file=None) is None


def test_parse_secrets_file_missing_is_empty(tmp_path):
    assert parse_secrets_file(tmp_path / "nope", (NAME,)) == {}


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #

def test_anyip_password_cmd_kwarg():
    ai = AnyIP("ab12", password_cmd="echo pw")
    assert ai.rotating() == "http://user_ab12:pw@portal.anyip.io:1080"


def test_anyip_username_cmd_kwarg_gets_prefixed():
    ai = AnyIP(username_cmd="echo ab12", password="pw")
    assert ai.username == "user_ab12"


def test_anyip_env_cmd(monkeypatch):
    monkeypatch.setenv("ANYIP_USERNAME", "ab12")
    monkeypatch.setenv("ANYIP_PASSWORD_CMD", "echo envcmdpw")
    assert AnyIP().rotating() == "http://user_ab12:envcmdpw@portal.anyip.io:1080"


def test_anyip_explicit_password_beats_cmd():
    ai = AnyIP("ab12", "explicit", password_cmd="exit 1")
    assert ai.password == "explicit"


def test_anyip_file_cmd(tmp_path):
    f = tmp_path / "secrets"
    f.write_text("ANYIP_USERNAME=ab12\nANYIP_PASSWORD_CMD=echo filepw\n")
    assert AnyIP(secrets_file=str(f)).password == "filepw"


def test_anyip_from_env_false_skips_file_cmd(tmp_path):
    f = tmp_path / "secrets"
    f.write_text("ANYIP_PASSWORD_CMD=echo filepw\n")
    assert AnyIP(from_env=False, secrets_file=str(f)).password is None


def test_anyip_failing_cmd_raises_at_construction():
    with pytest.raises(SecretCommandError, match="ANYIP_PASSWORD_CMD"):
        AnyIP("ab12", password_cmd="exit 1")


def test_dataimpulse_cmd_kwargs_and_env_cmd(monkeypatch):
    di = DataImpulse(username_cmd="echo acct", password_cmd="echo pw", country="il")
    assert di.rotating() == "http://acct__cr.il:pw@gw.dataimpulse.com:823"
    monkeypatch.setenv("DATAIMPULSE_USERNAME_CMD", "echo envacct")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD_CMD", "echo envpw")
    assert DataImpulse().rotating() == "http://envacct:envpw@gw.dataimpulse.com:823"


# --------------------------------------------------------------------------- #
# solver adapters
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cls, var", [(CapSolver, "CAPSOLVER_API_KEY"), (TwoCaptcha, "TWOCAPTCHA_API_KEY")])
def test_solver_key_from_env_and_env_cmd(monkeypatch, cls, var):
    monkeypatch.setenv(var, "envkey")
    assert cls().api_key == "envkey"
    monkeypatch.delenv(var)
    monkeypatch.setenv(var + "_CMD", "echo cmdkey")
    assert cls().api_key == "cmdkey"


@pytest.mark.parametrize("cls", [CapSolver, TwoCaptcha])
def test_solver_key_cmd_kwarg_and_explicit_precedence(cls):
    assert cls(api_key_cmd="echo k").api_key == "k"
    assert cls("explicit", api_key_cmd="exit 1").api_key == "explicit"


@pytest.mark.parametrize("cls", [CapSolver, TwoCaptcha])
def test_solver_missing_key_error_names_env_var(cls):
    with pytest.raises(ValueError, match=cls.ENV_API_KEY):
        cls()


def test_custom_solver_without_env_fallback_still_requires_key():
    class Mine(SolverService):
        def solve(self, sitekey, url, action="submit", **kw):  # pragma: no cover
            return ""

    with pytest.raises(ValueError):
        Mine()
    assert Mine(api_key_cmd="echo k").api_key == "k"


# --------------------------------------------------------------------------- #
# CLI flags
# --------------------------------------------------------------------------- #

def _ns(**kw) -> argparse.Namespace:
    base = dict(
        proxy=None, dataimpulse=False, anyip=False, proxy_country=None, proxy_network=None,
        proxy_username_cmd=None, proxy_password_cmd=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_cli_cmd_flags_feed_the_provider():
    url = cli._resolve_proxy(
        _ns(anyip=True, proxy_username_cmd="echo ab12", proxy_password_cmd="echo pw", proxy_country="il")
    )
    assert url == "http://user_ab12,country_IL:pw@portal.anyip.io:1080"


def test_cli_failing_cmd_is_a_clean_exit():
    with pytest.raises(SystemExit) as ei:
        cli._resolve_proxy(_ns(anyip=True, proxy_username_cmd="echo ab12", proxy_password_cmd="exit 7"))
    assert "ANYIP_PASSWORD_CMD" in str(ei.value) and "exited 7" in str(ei.value)


def test_cli_parser_accepts_cmd_flags():
    ns = cli.build_parser().parse_args(
        ["launch", "https://example.com", "--dataimpulse",
         "--proxy-username-cmd", "echo acct", "--proxy-password-cmd", "echo pw"]
    )
    assert cli._resolve_proxy(ns) == "http://acct:pw@gw.dataimpulse.com:823"
