"""Starts/reuses the persistent exchange-mcp-server HTTP instance for the smoke suite.

The suite never stops this process itself: start_server() launches it detached
(survives after this Python process exits) the first time it's needed, and
every later call reuses it by checking whether the port is already listening.
Only an explicit `python -m tests.smoke.server_manager stop` tears it down.

`EXCHANGE_SMOKE_HOST`/`EXCHANGE_SMOKE_PORT` point the suite at a server that is
*already* running elsewhere (e.g. a long-lived instance on another port) instead
of spawning one here. That matters because two servers cannot share one browser
profile directory, so spawning a second server while another already owns the
default profile fails or, worse, disturbs the live session. Since 2026-09-15 that
failure is at least legible: the second server detects the held profile and says
so (`ProfileLockedError`, issue #11) instead of reporting a generic closed-context
error. Note it is the *server* that refuses -- Playwright's Chromium does not,
which is exactly why the check had to be written. Reuse the running one instead:
    EXCHANGE_SMOKE_PORT=8767 python -m tests.smoke.tests.test_copilot

**Reuse is deliberate, but it is not free, and it used to be silent.** Attaching to
a server this process didn't start means every launch-time setting in *this*
environment is ignored: `EXCHANGE_BROWSER_PROFILE_DIR`, `--show-browser`, and the
tree the code is served from all belong to whoever started it. Worse, the port
being open says nothing about *which* checkout is behind it -- concurrent worktree
sessions routinely run their own instances on their own ports (8766 is the user's
production server and is never to be touched; 8765 is the shared dev/test port).

That combination produced a near-miss on 2026-09-14: a run with
`EXCHANGE_SMOKE_PORT=8767` attached to a server another worktree had started
minutes earlier (its own `_wt_launcher.py`, holding the lock on the main
checkout's `.browser-profile-dev`). The tests exercised *that* worktree's code, the
`EXCHANGE_BROWSER_PROFILE_DIR` passed on the command line did nothing at all, and
the green run was nearly written up as "verified on an independently launched
server with an isolated profile". A passing smoke run proves nothing about the tree
under test unless you know who owns the server.

So `ensure_server()` now says loudly which of the two happened, and identifies the
foreign server as far as it can (see `_warn_reusing_server`). The reuse behaviour
itself is unchanged -- it's correct, for the profile-lock reason above.
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOST = os.environ.get("EXCHANGE_SMOKE_HOST", "127.0.0.1")
PORT = int(os.environ.get("EXCHANGE_SMOKE_PORT", "8765"))
SERVER_URL = f"http://{HOST}:{PORT}/mcp"

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(__file__).resolve().parent / ".state"
PID_FILE = STATE_DIR / "server.pid"
LOG_FILE = STATE_DIR / "server.log"

START_TIMEOUT_SECONDS = 90

# CLAUDE.md: "Verify with a list_tools count of 62 before trusting a run." A count
# of 0 is the classic `python -m exchange_mcp.server` double-MCPServer mistake; any
# other mismatch means the server is serving a tree with a different tool set than
# this one -- which is precisely what a reused foreign server can silently be.
EXPECTED_TOOL_COUNT = 62

IDENTITY_PROBE_TIMEOUT_SECONDS = 15

# Announce reuse once per process, not once per ensure_server() call: a test module
# that opens more than one MCP session calls it repeatedly, and the answer can't
# change mid-run (the port doesn't switch owners under us). Without this the block
# repeats and pays for a redundant identity probe each time.
_reuse_announced = False


def _note(message: str) -> None:
    """stderr, prefixed. stdout is reserved for the tests' own result lines."""
    print(f"[server_manager] {message}", file=sys.stderr)


def is_port_open(host: str = HOST, port: int = PORT, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _run_capture(argv: list[str], timeout: float = 10) -> str | None:
    """Run a diagnostic command, returning stripped stdout or None on any failure.

    Every caller here is best-effort identification: a missing tool, a denied
    query or a timeout must degrade to "couldn't determine", never break a run
    that was otherwise fine.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    return out or None


def _listening_pid(host: str = HOST, port: int = PORT) -> int | None:
    """PID owning the listening socket, best-effort."""
    if sys.platform == "win32":
        out = _run_capture([
            "powershell", "-NoProfile", "-Command",
            f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
            "-ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess",
        ])
    else:
        out = _run_capture(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"])
    if not out:
        return None
    try:
        return int(out.splitlines()[0].strip())
    except ValueError:
        return None


def _process_command_line(pid: int) -> str | None:
    """The process's command line, best-effort."""
    if sys.platform == "win32":
        return _run_capture([
            "powershell", "-NoProfile", "-Command",
            f"Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
            "| Select-Object -ExpandProperty CommandLine",
        ])
    return _run_capture(["ps", "-p", str(pid), "-o", "args="])


def _probe_server_identity() -> tuple[str, str, int] | None:
    """Ask the running server what it is: (name, version, tool count).

    This is the only signal here that describes the *code being served* rather
    than the process wrapping it, which matters because a command line like
    `python _wt_launcher.py --port 8767` carries a relative script path and so
    reveals nothing about the server's cwd -- exactly the gap that let a foreign
    worktree go unnoticed.

    Uses the real SDK client rather than hand-rolled JSON-RPC, so protocol
    details (SSE framing, the initialized notification, session teardown) stay
    the SDK's problem. Runs on its own thread with its own event loop because
    `ensure_server()` is sync but is itself called from async code in
    `mcp_client.session()` -- `asyncio.run()` inline would raise there. Costs one
    short-lived MCP session and no OWA/mailbox traffic at all.
    """
    # Imported here, not at module scope: this is diagnostic-only, and the module
    # is also imported by `stop`/`status` invocations that need no MCP client.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def _probe() -> tuple[str, str, int]:
        # mcp 2.x: renamed transport function, two streams instead of three, and
        # snake_case result fields (`serverInfo` -> `server_info`). The version is
        # now exchange_mcp's own (server.py passes it explicitly); under 1.x this
        # line reported the *SDK's* version for every server.
        async with streamable_http_client(SERVER_URL) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                listing = await s.list_tools()
                return (init.server_info.name, init.server_info.version,
                        len(listing.tools))

    def _runner() -> tuple[str, str, int]:
        return asyncio.run(
            asyncio.wait_for(_probe(), IDENTITY_PROBE_TIMEOUT_SECONDS)
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_runner).result(IDENTITY_PROBE_TIMEOUT_SECONDS + 5)
    except Exception:
        return None


def _tracked_pid() -> int | None:
    """PID this harness recorded when it last launched a server, if any."""
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _warn_reusing_server() -> None:
    """Say, loudly, that we attached to a server this process did not start.

    Everything below is best-effort description; none of it gates the run. The
    goal is that a reader of the log can always answer "whose server was that,
    and were my settings in effect?" -- previously both were unanswerable.
    """
    pid = _listening_pid()
    tracked = _tracked_pid()
    ours = pid is not None and pid == tracked

    if ours:
        _note(f"Reusing the server this harness started earlier (pid {pid}) at {SERVER_URL}.")
    else:
        _note(f"REUSING a server this process did NOT start, at {SERVER_URL}.")
        _note("  Launch-time settings in THIS environment are NOT in effect: "
              "EXCHANGE_BROWSER_PROFILE_DIR, --show-browser and the served code")
        _note("  all belong to whoever started it. Nothing below was launched here.")

    if pid is None:
        _note("  Listening pid: could not be determined.")
    else:
        owner = (f"tracked in {PID_FILE.name}" if ours
                 else f"foreign -- {PID_FILE.name} says "
                      + (f"pid {tracked}" if tracked is not None
                         else "nothing was started here"))
        _note(f"  Listening pid {pid} ({owner}).")
        cmdline = _process_command_line(pid)
        if cmdline:
            _note(f"  Command line: {cmdline}")
            if not ours:
                # Only worth saying for a foreign server: for our own we printed
                # the tree at launch. And it needs saying at all because the
                # 2026-09-14 near-miss ran `python _wt_launcher.py --port 8767`,
                # whose relative path names no directory whatsoever.
                _note("  NOTE: the command line need not reveal the server's cwd "
                      "(a relative script path names no tree), so treat the "
                      "identity probe below as the authority on what it serves.")
        else:
            _note("  Command line: could not be determined.")

    identity = _probe_server_identity()
    if identity is None:
        _note("  Identity probe (initialize + tools/list) failed -- could not "
              "determine what this server is serving.")
    else:
        name, version, tool_count = identity
        suffix = ("" if tool_count == EXPECTED_TOOL_COUNT
                  else f"  <-- MISMATCH, expected {EXPECTED_TOOL_COUNT}; this server is "
                       "serving a different tree or registered no tools")
        _note(f"  Serving: {name} {version}, {tool_count} tools.{suffix}")

    if not ours:
        _note("  To confirm which browser profile it actually uses, call "
              "check_session and read its `cookie_file` field.")


def ensure_server() -> None:
    """Reuse the server if it's already listening; otherwise start it and wait.

    Reuse is never silent (see the module docstring): which of the two branches
    ran decides whether this environment's profile/browser settings mean anything,
    and whether the code under test is even this checkout's.
    """
    global _reuse_announced
    if is_port_open():
        if not _reuse_announced:
            _reuse_announced = True
            _warn_reusing_server()
        return
    _start_detached()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_port_open():
            return
        time.sleep(1)
    raise TimeoutError(
        f"exchange-mcp-server did not start listening on {HOST}:{PORT} within "
        f"{START_TIMEOUT_SECONDS}s. Check {LOG_FILE} for details."
    )


def _start_detached() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8")
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives this Python
        # process and isn't killed by Ctrl+C sent to it.
        creationflags = 0x00000008 | 0x00000200
    proc = subprocess.Popen(
        # Launch form matters here, and both obvious spellings are wrong.
        #
        # NOT the `exchange-mcp-server` console script: its sys.path[0] is the
        # interpreter's Scripts/ directory, so an editable install resolves
        # `exchange_mcp` to whatever path `pip install -e` recorded -- the main
        # working tree. Run from a git worktree that silently tested the *main*
        # tree instead of the one being edited, and the run looked normal.
        #
        # NOT `python -m exchange_mcp.server` either: that runs server.py as
        # __main__, creating one MCPServer instance, and then server.py's tool
        # imports (see its "Import tool modules" block) do
        # `from exchange_mcp.server import mcp`, loading the module a *second*
        # time under its real name and creating a second instance. Every
        # @mcp.tool() registers on that one while main() serves the __main__
        # one, so the server starts cleanly and answers "Unknown tool" to
        # everything.
        #
        # `-c` with a canonical import gets both right: cwd (REPO_ROOT, below)
        # is sys.path[0], and the module is imported once under its real name.
        [sys.executable, "-c", "from exchange_mcp.server import main; main()",
         "--transport", "http", "--port", str(PORT), "--show-browser"],
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    _note(f"LAUNCHED exchange_mcp.server from {REPO_ROOT} (pid={proc.pid}), "
          f"logging to {LOG_FILE}")
    # The counterpart to _warn_reusing_server's warning: on this path the
    # environment's settings *do* apply, and naming the tree and profile is what
    # makes the two log lines tell different stories at a glance.
    _note("  This environment's settings ARE in effect. Profile dir: "
          + (os.environ.get("EXCHANGE_BROWSER_PROFILE_DIR")
             or "<server default, see its startup banner>"))


def status() -> str:
    """Human-readable state, with ownership spelled out rather than implied.

    The old version distinguished the two cases only by the presence of a PID
    file, and worded the foreign case as a parenthetical aside. It is the case
    that invalidates assumptions, so it gets the emphasis -- and the actual
    listening PID, since a stale PID file can also disagree with reality.
    """
    if not is_port_open():
        return "not running"

    pid = _listening_pid()
    tracked = _tracked_pid()
    pid_text = f"pid {pid}" if pid is not None else "pid unknown"

    if tracked is None:
        return (f"running on {SERVER_URL} ({pid_text}) -- NOT started by this harness "
                f"(no {PID_FILE.name}); its profile/browser settings and the code it "
                "serves are whatever it was launched with")
    if pid is not None and pid != tracked:
        return (f"running on {SERVER_URL} ({pid_text}) -- NOT started by this harness: "
                f"{PID_FILE.name} tracks pid {tracked}, which is not what holds the "
                "port (stale PID file, or someone else's server took it)")
    return f"running on {SERVER_URL} (started by this harness, tracked pid {tracked})"


def stop_server() -> None:
    """Stop only the server this harness started, and say so when there isn't one.

    Deliberately never kills whatever happens to hold the port: on a machine
    running a production instance and several worktree sessions, "stop the thing
    on this port" is how you take down someone else's server (or the user's).
    """
    tracked = _tracked_pid()
    if tracked is None:
        _note(f"No tracked PID in {PID_FILE.name} -- nothing to stop here.")
        if is_port_open():
            listening = _listening_pid()
            _note(f"  Something IS listening on {SERVER_URL}"
                  + (f" (pid {listening})" if listening is not None else "")
                  + " but this harness did not start it -- leaving it alone. "
                    "Stop it manually if it's really yours.")
        return

    listening = _listening_pid()
    if listening is not None and listening != tracked:
        # Killing the tracked pid is still right (it's ours), but the port staying
        # open afterwards would otherwise look like the stop had failed.
        _note(f"  Heads up: {PID_FILE.name} tracks pid {tracked}, but pid {listening} "
              f"holds {SERVER_URL}. Stopping only ours; the port will stay open.")

    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(tracked), "/T", "/F"], check=False)
    else:
        subprocess.run(["kill", str(tracked)], check=False)
    PID_FILE.unlink(missing_ok=True)
    _note(f"Stopped pid {tracked}.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        ensure_server()
        print(status())
    elif cmd == "status":
        print(status())
    elif cmd == "stop":
        stop_server()
    else:
        print(f"Usage: python -m tests.smoke.server_manager [start|status|stop]", file=sys.stderr)
        sys.exit(1)
