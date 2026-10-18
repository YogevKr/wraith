"""A background browser session that ``wraith call`` commands share.

``wraith session start`` runs this module in a background process. The process
holds one browser, the same one the MCP server drives (:mod:`wraith.mcp`), and
answers requests on a Unix socket. So ``wraith call navigate URL``, then
``wraith call click 3``, then ``wraith call read`` all use one browser, not a
new browser for each command.

* Every MCP tool is a ``wraith call`` tool with the same arguments: required
  parameters are positional, the others are ``--flags``.
* Only the user who started a session can connect to it: its socket is in a
  folder with mode 0700 (``~/.wraith/sessions``, or ``WRAITH_SESSION_DIR``).
* A session closes its browser and exits after ``idle_timeout`` seconds with
  no call (default 30 minutes; 0 keeps it running).
* The start command sends the launch options on stdin, so a proxy password
  never shows in a process list.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import types
import typing
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

DEFAULT_NAME = "default"
DEFAULT_IDLE_TIMEOUT = 1800.0
ENV_SESSION_DIR = "WRAITH_SESSION_DIR"

#: Tool parameters that name local files. A relative value resolves against
#: the caller's working folder, not the session's.
_PATH_PARAMS = frozenset({"path", "paths", "session_file"})
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_SOCKET_PATH_MAX = 100  # macOS allows 104 bytes for a Unix socket path
_START_WAIT = 30.0
_CALL_WAIT = 3600.0


class SessionError(RuntimeError):
    """A session cannot be reached, started, or stopped."""


class SessionNotRunning(SessionError):
    """No session answers on the socket."""


class SessionClosed(SessionError):
    """The session closed the connection without a reply (it exited)."""


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def session_dir() -> Path:
    """The folder for session sockets and logs; only its owner may use it."""
    custom = os.environ.get(ENV_SESSION_DIR)
    folder = Path(custom) if custom else Path.home() / ".wraith" / "sessions"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.stat().st_mode & 0o077:
        raise SessionError(f"{folder} is open to other users; run `chmod 700 {folder}`")
    return folder


def _check_name(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise SessionError(
            f"bad session name {name!r}: use 1 to 40 letters, digits, '.', '_' or '-'"
        )
    return name


def socket_path(name: str) -> Path:
    path = session_dir() / f"{_check_name(name)}.sock"
    if len(str(path).encode()) > _SOCKET_PATH_MAX:
        raise SessionError(
            f"the socket path {path} is too long for a Unix socket; "
            f"set {ENV_SESSION_DIR} to a shorter folder"
        )
    return path


def log_path(name: str) -> Path:
    return session_dir() / f"{_check_name(name)}.log"


def _require_unix() -> None:
    if not hasattr(socket, "AF_UNIX") or sys.platform == "win32":
        raise SessionError("background sessions need Unix sockets (macOS or Linux)")


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _exchange(name: str, request: dict, timeout: Optional[float]) -> dict:
    """Send one request to session ``name`` and return its reply."""
    _require_unix()
    path = socket_path(name)
    chunks: list[bytes] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(str(path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise SessionNotRunning(f"session {name!r} is not running") from exc
        try:
            conn.sendall(json.dumps(request).encode() + b"\n")
            while chunk := conn.recv(1 << 16):
                chunks.append(chunk)
        except socket.timeout as exc:
            raise SessionError(
                f"session {name!r} did not answer in {timeout:.0f} s"
            ) from exc
        except (BrokenPipeError, ConnectionResetError):
            chunks = []
    if not chunks:
        raise SessionClosed(f"session {name!r} closed the connection without a reply")
    return json.loads(b"".join(chunks))


def ping(name: str = DEFAULT_NAME) -> Optional[dict]:
    """The session's status, or ``None`` when it is not running."""
    try:
        return _exchange(name, {"op": "ping"}, timeout=10.0)
    except (SessionNotRunning, SessionClosed):
        # A session that is exiting can accept a connection and then close it.
        return None


@contextlib.contextmanager
def _start_lock(name: str) -> Iterator[None]:
    """Let one process at a time start session ``name``."""
    import fcntl

    with open(session_dir() / f"{_check_name(name)}.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _log_tail(name: str, lines: int = 15) -> str:
    try:
        text = log_path(name).read_text(errors="replace")
    except OSError:
        return "(no log)"
    return "\n".join(text.strip().splitlines()[-lines:]) or "(empty log)"


def _spawn(name: str, config: dict, wait: float) -> dict:
    """Start the session process and wait until it answers."""
    with log_path(name).open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "wraith.daemon"],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps({**config, "name": name}).encode())
    proc.stdin.close()
    deadline = time.monotonic() + wait
    while True:
        info = ping(name)
        if info is not None:
            return info
        if proc.poll() is not None:
            raise SessionError(
                f"session {name!r} exited at start (code {proc.returncode}):\n{_log_tail(name)}"
            )
        if time.monotonic() > deadline:
            proc.kill()
            raise SessionError(
                f"session {name!r} did not start in {wait:.0f} s:\n{_log_tail(name)}"
            )
        time.sleep(0.1)


def start(
    name: str = DEFAULT_NAME,
    *,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    launch: Optional[dict] = None,
    allowed_domains: Optional[str] = None,
    upload_dirs: Optional[list[str]] = None,
) -> dict:
    """Start session ``name`` in the background and return its status.

    ``launch`` holds the browser launch options (``engine``, ``headless``,
    ``geoip``, ``proxy``). The browser itself starts on the first call.
    """
    _require_unix()
    config = {
        "idle_timeout": idle_timeout,
        "launch": {k: v for k, v in (launch or {}).items() if v is not None},
        "allowed_domains": allowed_domains,
        "upload_dirs": upload_dirs,
    }
    with _start_lock(name):
        if ping(name) is not None:
            raise SessionError(f"session {name!r} is already running")
        return _spawn(name, config, _START_WAIT)


def ensure(name: str = DEFAULT_NAME) -> tuple[dict, bool]:
    """Session ``name``'s status, starting it with defaults when needed.

    Returns ``(status, started)``. Two callers at the same time get one session.
    """
    info = ping(name)
    if info is not None and not info.get("stopping"):
        return info, False
    _require_unix()
    with _start_lock(name):
        _wait_gone(name, 60.0, stopping_only=True)
        info = ping(name)
        if info is not None:
            return info, False
        return _spawn(name, {"idle_timeout": DEFAULT_IDLE_TIMEOUT}, _START_WAIT), True


def call(name: str, tool: str, argv: list[str], cwd: Optional[str] = None) -> dict:
    """Run ``tool`` with command-line ``argv`` in session ``name``.

    The reply has ``ok``, ``output`` (the tool's text, help, or error), and
    ``status`` (the exit code for the CLI).
    """
    request = {
        "op": "call",
        "tool": tool,
        "argv": list(argv),
        "cwd": cwd or os.getcwd(),
    }
    return _exchange(name, request, timeout=_CALL_WAIT)


def tools(name: str = DEFAULT_NAME) -> str:
    """The tool list of session ``name``, one line for each tool."""
    return _exchange(name, {"op": "tools"}, timeout=10.0)["output"]


def _wait_gone(name: str, wait: float, stopping_only: bool = False) -> None:
    """Wait until session ``name`` no longer answers.

    The session answers until its browser is closed. With ``stopping_only``,
    a session that is not stopping counts as done.
    """
    deadline = time.monotonic() + wait
    while (info := ping(name)) is not None:
        if stopping_only and not info.get("stopping"):
            return
        if time.monotonic() > deadline:
            raise SessionError(f"session {name!r} did not stop in {wait:.0f} s")
        time.sleep(0.1)


def stop(name: str = DEFAULT_NAME, wait: float = 60.0) -> bool:
    """Stop session ``name`` and close its browser. False when it was not running."""
    try:
        _exchange(name, {"op": "stop"}, timeout=10.0)
    except SessionNotRunning:
        return False
    _wait_gone(name, wait)
    return True


def list_sessions() -> list[tuple[str, Optional[dict]]]:
    """Every session with a socket: ``(name, status)``; status ``None`` if dead."""
    _require_unix()
    found = []
    for path in sorted(session_dir().glob("*.sock")):
        found.append((path.stem, ping(path.stem)))
    return found


# --------------------------------------------------------------------------- #
# Tool arguments: built from each tool's signature
# --------------------------------------------------------------------------- #
class _ParseExit(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that returns its help and errors instead of exiting."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.printed: list[str] = []

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            self.printed.append(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> typing.NoReturn:
        if message:
            self.printed.append(message)
        raise _ParseExit(status, "".join(self.printed))


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return value


def _arg_kind(annotation: Any) -> tuple[Callable[[str], Any], bool]:
    """The argparse ``type`` for ``annotation``, and whether it takes many values."""
    is_union = typing.get_origin(annotation) is typing.Union or isinstance(
        annotation, getattr(types, "UnionType", ())
    )
    if is_union:
        options = [a for a in typing.get_args(annotation) if a is not type(None)]
        annotation = options[0] if len(options) == 1 else str
    origin = typing.get_origin(annotation) or annotation
    if origin is list:
        inner = typing.get_args(annotation)
        return (inner[0] if inner and inner[0] in (int, float, str) else str), True
    if origin is dict:
        return _json_object, False
    if annotation in (int, float, str, bool):
        return annotation, False
    return str, False


def tool_parser(name: str, fn: Callable[..., Any]) -> _Parser:
    """A parser for ``wraith call NAME``, built from ``fn``'s signature."""
    parser = _Parser(
        prog=f"wraith call {name}",
        description=inspect.getdoc(fn),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    hints = typing.get_type_hints(fn)
    for pname, param in inspect.signature(fn).parameters.items():
        kind, many = _arg_kind(hints.get(pname, str))
        nargs = "+" if many else None
        flag = "--" + pname.replace("_", "-")
        if param.default is inspect.Parameter.empty:
            parser.add_argument(pname, type=kind, nargs=nargs)
        elif kind is bool:
            parser.add_argument(
                flag,
                dest=pname,
                default=param.default,
                action=argparse.BooleanOptionalAction,
            )
        else:
            parser.add_argument(
                flag,
                dest=pname,
                type=kind,
                nargs=nargs,
                default=param.default,
                metavar=pname.upper(),
            )
    if name == "screenshot":
        parser.add_argument(
            "--out",
            default=None,
            metavar="PNG",
            help="file to write (default: wraith-screenshot-<time>.png here)",
        )
    return parser


def _under(cwd: str, value: str) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else Path(cwd) / path)


def _resolve_paths(kwargs: dict, cwd: str) -> None:
    """Make the file arguments absolute against the caller's folder."""
    for key in _PATH_PARAMS & kwargs.keys():
        value = kwargs[key]
        if isinstance(value, list):
            kwargs[key] = [_under(cwd, item) for item in value]
        elif isinstance(value, str) and value:
            kwargs[key] = _under(cwd, value)


def _save_image(result: Any, out: Path) -> str:
    """Write a screenshot result (an MCP Image or a temp PNG path) to ``out``."""
    data = getattr(result, "data", None)
    if data is None and isinstance(result, str) and os.path.isfile(result):
        data = Path(result).read_bytes()
        os.unlink(result)
    if not isinstance(data, (bytes, bytearray)):
        return str(result)
    out.write_bytes(data)
    return f"saved {out} ({len(data)} bytes)"


def _render(result: Any) -> tuple[str, int]:
    """A tool result as CLI text and an exit status."""
    if isinstance(result, bool):
        return ("true", 0) if result else ("false", 1)
    if isinstance(result, (list, tuple)):
        return "\n".join(str(item) for item in result), 0
    return ("" if result is None else str(result)), 0


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class _Browser:
    """The MCP server's shared browser, as the session server sees it."""

    def is_open(self) -> bool:
        from . import mcp

        return mcp._browser is not None

    async def close(self) -> None:
        from . import mcp

        if mcp._browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(mcp._run(mcp._reset_browser), 60)


class _Server:
    """Answers ping / tools / call / stop requests for one session."""

    def __init__(
        self,
        name: str,
        tools: dict[str, Callable[..., Any]],
        idle_timeout: float,
        browser: "_Browser",
    ) -> None:
        self.name = name
        self.tools = tools
        self.idle_timeout = idle_timeout
        self.browser = browser
        self.started = time.time()
        self._last = time.monotonic()
        self._busy = 0
        self._stopping = False
        self._stop: Optional[asyncio.Event] = None

    def info(self) -> dict:
        return {
            "name": self.name,
            "pid": os.getpid(),
            "started": self.started,
            "idle_timeout": self.idle_timeout,
            "idle_for": round(time.monotonic() - self._last, 1),
            "browser_open": self.browser.is_open(),
            "stopping": self._stopping,
        }

    def _tool_list(self) -> str:
        lines = ["Tools (`wraith call TOOL --help` shows the arguments):"]
        for name in sorted(self.tools):
            doc = " ".join(
                (inspect.getdoc(self.tools[name]) or "").split("\n\n")[0].split()
            )
            lines.append(f"  {name:<18} {doc[:100]}")
        return "\n".join(lines)

    async def run(self, path: Path) -> None:
        self._stop = asyncio.Event()
        old_umask = os.umask(0o077)
        try:
            server = await asyncio.start_unix_server(
                self._handle, path=str(path), limit=1 << 20
            )
        finally:
            os.umask(old_umask)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self._stop.set)
        idle = asyncio.ensure_future(self._watch_idle())
        try:
            await self._stop.wait()
        finally:
            # Close the browser while the socket still answers, so `stop()`
            # returns only after the browser is gone. Calls are refused now.
            idle.cancel()
            self._stopping = True
            await self.browser.close()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            server.close()

    async def _watch_idle(self) -> None:
        if self.idle_timeout <= 0:
            return
        assert self._stop is not None
        while True:
            await asyncio.sleep(min(5.0, self.idle_timeout / 2))
            if self._busy == 0 and time.monotonic() - self._last >= self.idle_timeout:
                print(
                    f"session {self.name}: no call for {self.idle_timeout:.0f} s, stopping",
                    flush=True,
                )
                self._stop.set()
                return

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request: dict = {}
        try:
            request = json.loads(await reader.readline() or b"{}")
            reply = await self._answer(request)
        except Exception as exc:
            reply = {"ok": False, "output": f"{type(exc).__name__}: {exc}", "status": 1}
        try:
            writer.write(json.dumps(reply).encode())
            await writer.drain()
        finally:
            writer.close()
        if request.get("op") == "stop" and self._stop is not None:
            self._stop.set()

    async def _answer(self, request: dict) -> dict:
        op = request.get("op")
        if op == "ping":
            return {"ok": True, **self.info()}
        if op == "tools":
            return {"ok": True, "output": self._tool_list(), "status": 0}
        if op == "stop":
            return {
                "ok": True,
                "output": f"stopping session {self.name!r}",
                "status": 0,
            }
        if op == "call" and self._stopping:
            return {
                "ok": False,
                "output": f"session {self.name!r} is stopping; try again",
                "status": 1,
            }
        if op == "call":
            return await self._call(
                str(request.get("tool", "")),
                [str(a) for a in request.get("argv", [])],
                str(request.get("cwd") or os.getcwd()),
            )
        return {"ok": False, "output": f"unknown request {op!r}", "status": 2}

    async def _call(self, tool: str, argv: list[str], cwd: str) -> dict:
        fn = self.tools.get(tool)
        if fn is None:
            known = ", ".join(sorted(self.tools))
            return {
                "ok": False,
                "output": f"unknown tool {tool!r}. Tools: {known}",
                "status": 2,
            }
        try:
            kwargs = vars(tool_parser(tool, fn).parse_args(argv))
        except _ParseExit as exc:
            return {
                "ok": exc.status == 0,
                "output": exc.text.rstrip(),
                "status": exc.status,
            }
        out = kwargs.pop("out", None) if tool == "screenshot" else None
        _resolve_paths(kwargs, cwd)
        self._busy += 1
        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**kwargs)
            else:
                result = await asyncio.to_thread(fn, **kwargs)
        except Exception as exc:
            text = (
                str(exc)
                if type(exc).__name__ == "ToolError"
                else f"{type(exc).__name__}: {exc}"
            )
            return {"ok": False, "output": text, "status": 1}
        finally:
            self._busy -= 1
            self._last = time.monotonic()
        if tool == "screenshot":
            name = out or time.strftime("wraith-screenshot-%Y%m%d-%H%M%S.png")
            text, status = _save_image(result, Path(_under(cwd, name))), 0
        else:
            text, status = _render(result)
        return {"ok": status == 0, "output": text, "status": status}


async def _serve(name: str, config: dict) -> None:
    """Run session ``name`` over the MCP server's tools and shared browser."""
    from . import mcp

    launch = mcp._launch_kw_from_env()
    launch.update(config.get("launch") or {})
    mcp.configure(**launch)
    if config.get("allowed_domains"):
        mcp.set_allowed_domains(config["allowed_domains"])
    if config.get("upload_dirs"):
        mcp.set_upload_dirs(config["upload_dirs"])
    tools = {tool.name: getattr(mcp, tool.name) for tool in await mcp.app.list_tools()}
    idle_timeout = float(config.get("idle_timeout", DEFAULT_IDLE_TIMEOUT))
    server = _Server(name, tools, idle_timeout, _Browser())
    path = socket_path(name)
    print(f"session {name}: pid {os.getpid()}, socket {path}", flush=True)
    await server.run(path)
    print(f"session {name}: stopped", flush=True)


def main() -> None:
    """Entry point of the background process; the config arrives on stdin."""
    config = json.loads(sys.stdin.read() or "{}")
    asyncio.run(_serve(_check_name(config.get("name", DEFAULT_NAME)), config))


if __name__ == "__main__":
    main()
