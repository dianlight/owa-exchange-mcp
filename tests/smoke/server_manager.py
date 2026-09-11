"""Starts/reuses the persistent exchange-mcp-server HTTP instance for the smoke suite.

The suite never stops this process itself: start_server() launches it detached
(survives after this Python process exits) the first time it's needed, and
every later call reuses it by checking whether the port is already listening.
Only an explicit `python -m tests.smoke.server_manager stop` tears it down.

`EXCHANGE_SMOKE_HOST`/`EXCHANGE_SMOKE_PORT` point the suite at a server that is
*already* running elsewhere (e.g. a long-lived instance on another port) instead
of spawning one here. That matters because two servers cannot share one browser
profile directory -- Chromium holds an exclusive lock on it -- so spawning a
second server while another already owns the default profile fails or, worse,
disturbs the live session. Reuse the running one instead:
    EXCHANGE_SMOKE_PORT=8767 python -m tests.smoke.tests.test_copilot
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HOST = os.environ.get("EXCHANGE_SMOKE_HOST", "127.0.0.1")
PORT = int(os.environ.get("EXCHANGE_SMOKE_PORT", "8765"))
SERVER_URL = f"http://{HOST}:{PORT}/mcp"

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(__file__).resolve().parent / ".state"
PID_FILE = STATE_DIR / "server.pid"
LOG_FILE = STATE_DIR / "server.log"

START_TIMEOUT_SECONDS = 90


def is_port_open(host: str = HOST, port: int = PORT, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_server() -> None:
    """Reuse the server if it's already listening; otherwise start it and wait."""
    if is_port_open():
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
        ["exchange-mcp-server", "--transport", "http", "--port", str(PORT), "--show-browser"],
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"[server_manager] Launched exchange-mcp-server (pid={proc.pid}), "
          f"logging to {LOG_FILE}", file=sys.stderr)


def status() -> str:
    if not is_port_open():
        return "not running"
    pid_note = f" (tracked pid {PID_FILE.read_text().strip()})" if PID_FILE.exists() else " (not started by this harness)"
    return f"running on {SERVER_URL}{pid_note}"


def stop_server() -> None:
    if not PID_FILE.exists():
        print("[server_manager] No tracked PID file — nothing to stop (or it wasn't "
              "started by this harness). Stop it manually if needed.", file=sys.stderr)
        return
    pid = PID_FILE.read_text().strip()
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], check=False)
    else:
        subprocess.run(["kill", pid], check=False)
    PID_FILE.unlink(missing_ok=True)
    print(f"[server_manager] Stopped pid {pid}.", file=sys.stderr)


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
