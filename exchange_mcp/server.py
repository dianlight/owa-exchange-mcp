"""Exchange MCP Server.

Exposes OWA email, calendar, directory, and availability tools via MCP.
Uses the mcp SDK's MCPServer (v2; `FastMCP` in v1) with a lifespan context
manager to share a single OWAClient (backed by one persistent browser session)
across all tool invocations.
"""

import argparse
import asyncio
import atexit
import os
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from exchange_mcp import auth_errors
from exchange_mcp import __version__
from exchange_mcp.browser_session import BrowserSession, is_source_checkout
from exchange_mcp.owa_client import OWAClient


@dataclass
class AppContext:
    """Shared application state available to all tools via lifespan context.

    `pending_login` is backed by the module-level `_shared_pending_login`
    (see _ensure_started) rather than a per-instance field, because the login
    tool's two-call 2FA flow needs the *second* call — which may arrive on a
    different MCP client session than the first — to see the background task
    the first call started.

    Under mcp 1.x that was the only thing making the flow work at all: the
    streamable-http session manager entered `app_lifespan()` once *per client
    session*, so a per-instance field was invisible to every other session.
    mcp 2.x enters the lifespan once per `StreamableHTTPSessionManager.run()`
    and shares the result across all sessions, so a single AppContext now
    serves them all and a plain field would suffice. It stays module-level
    anyway: that is one fewer behaviour depending on an SDK detail that has
    already changed once, and it keeps stdio and http identical.
    """
    client: OWAClient

    @property
    def pending_login(self) -> "asyncio.Task | None":
        return _shared_pending_login

    @pending_login.setter
    def pending_login(self, value: "asyncio.Task | None") -> None:
        global _shared_pending_login
        _shared_pending_login = value


def _resolve_headless() -> bool:
    if "--show-browser" in sys.argv:
        return False
    env = os.environ.get("EXCHANGE_HEADLESS", "").strip().lower()
    if env in ("false", "0", "no"):
        return False
    return True


def _load_env_file() -> None:
    """Populate os.environ from .env.local, without overriding vars already set.

    Lets an unattended autostart process (Windows Task Scheduler, no shell
    `export` to inherit from) pick up EXCHANGE_OWA_URL and friends the same way
    an MCP client's stdio `env` block does today.
    """
    env_path = Path(__file__).parent.parent / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


# How long the startup sign-in window stays open waiting for a human, and how
# long the `login` tool's own window waits. Generous on purpose: a real sign-in
# means typing an address, a password, and approving a push on a phone.
LOGIN_WINDOW_SECONDS = int(os.environ.get("EXCHANGE_LOGIN_TIMEOUT", "300"))

_shared_state_lock = threading.Lock()
_shared_browser: BrowserSession | None = None
_shared_client: OWAClient | None = None
_shared_startup_thread: threading.Thread | None = None
_shared_pending_login: asyncio.Task | None = None


def _log(message: str) -> None:
    """Everything goes to stderr - stdout is the stdio transport's JSON-RPC stream."""
    print(f"[exchange-mcp] {message}", file=sys.stderr, flush=True)


def _profile_dir_source() -> str:
    """Explain *why* the profile directory is what it is, for the startup banner.

    Worth logging explicitly: the source-checkout vs. installed-package split is
    invisible otherwise, and an editable install (`pip install -e .`) counts as a
    checkout - so someone expecting ~/owa-mcp/ after `pip install -e .` gets the
    repo path instead and has no way to tell why.
    """
    if os.environ.get("EXCHANGE_BROWSER_PROFILE_DIR"):
        return "EXCHANGE_BROWSER_PROFILE_DIR"
    if is_source_checkout():
        return "default for a source checkout (incl. `pip install -e .`)"
    return "default for an installed package"


def _log_startup_banner(browser: BrowserSession) -> None:
    """Report version and resolved configuration before anything can go wrong.

    Printed once per process, from _ensure_started(), so it lands ahead of the
    browser launch and the auth check on every entry path and both transports.
    """
    _log(f"exchange-mcp-server {__version__}")
    _log(f"OWA URL:     {browser.owa_url}")
    _log(f"Profile dir: {browser.profile_dir}")
    _log(f"  source:    {_profile_dir_source()}")
    _log(f"  state:     {'exists, reusing it' if browser.profile_existed else 'does not exist, will be created'}")
    _log(f"Browser:     {'headless' if browser.headless else 'visible window'}")


def _startup(browser: BrowserSession) -> None:
    """Launch the browser on the persistent profile and make sure it's signed in.

    Runs on a plain background thread (see _ensure_started) rather than inline or
    as an asyncio task, for two reasons:

    - A cold Chromium launch plus a human-paced interactive sign-in takes well
      past any MCP client's connect timeout, so it must not block the handshake -
      nor, under --transport http, the port opening (the smoke-test runner gives
      the server 90s to start listening, far less than a sign-in can take).
    - A thread works identically on both transports. An asyncio task would have
      to be created on whichever loop the transport happens to run, and under
      --transport http there *is* no such loop until a client connects. Every
      BrowserSession method is already synchronous (it owns its own loop on its
      own thread), so there is nothing to await here anyway.

    The flow is profile-first, with no credentials anywhere:
    1. Reuse the profile directory if it exists, create it if it doesn't.
    2. If that profile is still signed in (live OWA cookies, "stay signed in",
       or an SSO session the SPA can mint a token from) - done, serve.
    3. Otherwise open a *visible* browser window on the OWA sign-in page and wait
       for the user to complete it, 2FA included.
    4. If nobody completes it, keep serving anyway: tools report
       `authorization_required` and the `login` tool reopens the window on
       demand. A stdio server is often spawned while the user is away, and dying
       for that reason would be worse than waiting to be asked.
    """
    try:
        _log("Launching browser...")
        browser.start()

        if browser.has_active_session():
            _log("Auth status: AUTHENTICATED (the profile's OWA session is still valid). Ready.")
            return

        _log("Auth status: NOT AUTHENTICATED - opening a browser window on the OWA sign-in "
             f"page (waiting up to {LOGIN_WINDOW_SECONDS}s). Please sign in there, 2FA included.")
        result = browser.interactive_login(LOGIN_WINDOW_SECONDS)

        if result.get("success"):
            _log(f"Auth status: AUTHENTICATED. {result.get('message', 'Signed in successfully.')}")
            return

        reason = result.get("reason") or auth_errors.LOGIN_TIMEOUT
        _log(f"Auth status: NOT AUTHENTICATED ({reason}): {result.get('error')}")
        _log(auth_errors.remediation(reason))
        _log("The server keeps serving; tools will report that authorization is required "
             "until someone signs in.")
    except Exception as exc:
        _log(f"Auth status: UNKNOWN - startup failed: {exc}. The `login` tool remains available.")


def _ensure_started() -> OWAClient:
    """Create the shared BrowserSession/OWAClient and kick off startup, once.

    Called from main() at process start *and* from app_lifespan, because those are
    two genuinely different entry points:

    - main() runs it before any transport comes up. That is where a missing
      EXCHANGE_OWA_URL becomes a clean exit(2) instead of an exception raised
      inside a transport's lifespan, and it is the only path under --transport
      http that predates mcp 2.x: the 1.x session manager entered
      app_lifespan() per *client session*, so nothing at all happened - no
      browser, no profile directory, no sign-in window - until something
      connected. mcp 2.x enters the lifespan once, at ASGI startup, so that
      particular gap is gone; starting from main() is still what keeps the
      config check ahead of the port opening.
    - app_lifespan calls it too so that anything importing `mcp` and serving it
      directly (a test harness, an ASGI embed) gets the same setup instead of an
      uninitialized client.

    Guarded by a threading.Lock, not an asyncio.Lock: this now runs from main()'s
    bare thread, from a transport's event loop, or from either, and an
    asyncio.Lock binds itself to the first loop that touches it and then refuses
    every other one.
    """
    global _shared_browser, _shared_client, _shared_startup_thread

    with _shared_state_lock:
        if _shared_client is not None:
            return _shared_client

        owa_url = os.environ.get("EXCHANGE_OWA_URL", "")
        if not owa_url:
            raise ValueError("OWA URL not configured. Set the EXCHANGE_OWA_URL environment variable.")

        profile_dir = os.environ.get("EXCHANGE_BROWSER_PROFILE_DIR") or None
        browser = BrowserSession(owa_url, headless=_resolve_headless(), profile_dir=profile_dir)
        _shared_browser = browser
        _shared_client = OWAClient(browser)

        _log_startup_banner(browser)
        _shared_startup_thread = threading.Thread(
            target=_startup, args=(browser,), daemon=True, name="owa-startup"
        )
        _shared_startup_thread.start()
        return _shared_client


@atexit.register
def _stop_shared_browser() -> None:
    if _shared_browser is not None:
        _shared_browser.stop()


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """Yield the process-wide shared OWAClient (see _ensure_started).

    Normally a no-op beyond the lookup, because main() has already started
    everything before the transport came up. Deliberately does not stop the
    browser on the way out - only process exit (_stop_shared_browser, above)
    does that. Under mcp 2.x this runs once per server process on both
    transports (1.x re-ran it per streamable-http client session), so the
    yielded AppContext is shared by every session.
    """
    yield AppContext(client=_ensure_started())


# Create the MCP server instance. `version` is passed explicitly because mcp 2.x
# reports `serverInfo.version` exactly as given and defaults it to the empty
# string - 1.x quietly substituted the *SDK's* own version, which was never the
# right answer anyway. This is the number the smoke suite's identity probe and
# any client's server list display.
mcp = MCPServer("exchange", version=__version__, lifespan=app_lifespan)

# ------------------------------------------------------------------
# Import tool modules so their @mcp.tool() decorators register tools.
# Each module imports `mcp` from this file and decorates its functions.
# ------------------------------------------------------------------
import exchange_mcp.tools.email      # noqa: E402, F401
import exchange_mcp.tools.calendar   # noqa: E402, F401
import exchange_mcp.tools.people     # noqa: E402, F401
import exchange_mcp.tools.folders    # noqa: E402, F401
import exchange_mcp.tools.availability  # noqa: E402, F401
import exchange_mcp.tools.analytics     # noqa: E402, F401
import exchange_mcp.tools.auth          # noqa: E402, F401
import exchange_mcp.tools.categories     # noqa: E402, F401
import exchange_mcp.tools.copilot        # noqa: E402, F401
import exchange_mcp.tools.tasks          # noqa: E402, F401
import exchange_mcp.tools.discovery      # noqa: E402, F401

# Tools that reproducibly fail at call time (see PROJECT_STATUS.md KO rows) rather
# than merely untested or degraded-but-working ones (e.g. get_meeting_contacts, which
# returns an empty result plus a `warnings` field instead of failing). Excluded from
# the MCP tool listing under --stable so a client can't call them and hit a fault.
#
# The five Copilot tools (#901-905) used to be listed here: their chat-pane
# automation failed 100% of the time on a live bearer-mode tenant. All five pass
# tests/smoke/tests/test_copilot.py as of 2026-09-11 (cross-origin iframe, frame
# replacement race, answer extraction and prompt grounding all fixed - see the
# Copilot notes in PROJECT_STATUS.md), so they are no longer excluded under
# --stable. They remain the most fragile tools here, since they drive a DOM
# Microsoft can restyle without notice; the smoke test is the tripwire.
KNOWN_BUGGY_TOOLS: dict[str, str] = {}


def _apply_stable_mode() -> None:
    """Remove known-buggy tools from the MCP tool listing so --stable clients can't call them."""
    for name, reason in KNOWN_BUGGY_TOOLS.items():
        try:
            mcp.remove_tool(name)
            _log(f"--stable: excluded buggy tool '{name}' ({reason})")
        except Exception as exc:
            _log(f"--stable: could not exclude '{name}': {exc}")


def main():
    """Entry point: run the MCP server over stdio (default) or streamable-http."""
    _load_env_file()

    parser = argparse.ArgumentParser(description="Exchange MCP server")
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run the browser with a visible window instead of headless (for debugging).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.environ.get("EXCHANGE_MCP_TRANSPORT", "stdio"),
        help="Transport to serve over: 'stdio' (spawned per client, default) or "
             "'http' (persistent streamable-http server on --host/--port).",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("EXCHANGE_MCP_HOST", "127.0.0.1"),
        help="Bind host for --transport http. Keep this on loopback "
             "(127.0.0.1) — the MCP endpoint has no auth of its own.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EXCHANGE_MCP_PORT", "8765")),
        help="Bind port for --transport http.",
    )
    parser.add_argument(
        "--stable",
        action="store_true",
        default=os.environ.get("EXCHANGE_MCP_STABLE", "").strip().lower() in ("true", "1", "yes"),
        help="Exclude tools with a known, unfixable server-side bug (see PROJECT_STATUS.md) "
             "from the MCP tool listing, instead of exposing them to fail at call time.",
    )
    args = parser.parse_args()

    if args.stable:
        _apply_stable_mode()

    # Launch the browser and check/establish the OWA session now, at process
    # start, rather than leaving it to app_lifespan. Under --transport http the
    # lifespan doesn't run until a client connects, so waiting for it meant a
    # freshly started server did nothing at all - no profile directory, no
    # browser, no sign-in window - until something happened to connect.
    # _ensure_started() does its work on a background thread, so neither the
    # stdio handshake nor the http port opening is delayed by it.
    try:
        _ensure_started()
    except ValueError as exc:
        # Missing EXCHANGE_OWA_URL: fail loudly here instead of once per tool
        # call. Nothing is listening yet, so exiting is clean.
        _log(f"Configuration error: {exc}")
        raise SystemExit(2) from exc

    # Ctrl+C is the *documented* way to stop an http server (there is no shutdown
    # endpoint), so it must not look like a crash. anyio's asyncio backend cancels
    # the main task on SIGINT and then re-raises KeyboardInterrupt after uvicorn has
    # already drained cleanly; letting that escape printed a 20-line traceback and
    # exited 1, which is indistinguishable from a real fault to any supervisor.
    # Returning normally here still runs _stop_shared_browser via atexit.
    try:
        if args.transport == "http":
            _log(f"Transport:   streamable-http on http://{args.host}:{args.port}/mcp")
            # host/port are run() arguments in mcp 2.x, not constructor settings:
            # `mcp.settings.host = ...` raises ValueError there (Settings keeps only
            # the constructor-owned fields). Passing host here is also what arms
            # DNS-rebinding protection - the SDK auto-enables Host/Origin
            # validation when it is a loopback address, which is why --host must
            # stay on 127.0.0.1 (the MCP endpoint has no auth of its own).
            #
            # session_idle_timeout=None keeps mcp 1.x's behaviour: 2.x defaults to
            # reaping any session with no request in flight for 30 minutes, and a
            # reaped session id answers 404 "Session not found" - the *exact*
            # symptom reported as issue #18, which cost a full investigation to
            # attribute. This server is deliberately long-lived, loopback-only and
            # single-user, so the runaway-session leak the timeout guards against
            # isn't reachable here, while a client idling half an hour very much is.
            mcp.run(
                transport="streamable-http",
                host=args.host,
                port=args.port,
                session_idle_timeout=None,
            )
        else:
            _log("Transport:   stdio")
            mcp.run()
    except KeyboardInterrupt:
        _log("Interrupted (Ctrl+C) - shutting down browser session and exiting.")


if __name__ == "__main__":
    main()
