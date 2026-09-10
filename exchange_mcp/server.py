"""Exchange MCP Server.

Exposes OWA email, calendar, directory, and availability tools via MCP.
Uses FastMCP with a lifespan context manager to share a single OWAClient
(backed by one persistent browser session) across all tool invocations.
"""

import argparse
import asyncio
import atexit
import os
import sys
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning

# mcp's FastMCP.Settings has a `lifespan` field typed with a self-referential
# generic (Callable[[FastMCP[LifespanResultT]], ...]); pydantic-settings can't
# resolve it and warns on every FastMCP(...) construction. That field is never
# read from an env var, so the warning doesn't apply here — silence it.
warnings.filterwarnings("ignore", category=IncompleteFieldDefinitionWarning)

from mcp.server.fastmcp import FastMCP

from exchange_mcp import auth_errors
from exchange_mcp.browser_session import BrowserSession
from exchange_mcp.owa_client import OWAClient


@dataclass
class AppContext:
    """Shared application state available to all tools via lifespan context.

    `pending_login` is backed by the module-level `_shared_pending_login`
    (see _get_shared_client) rather than a per-instance field: AppContext
    itself is created fresh per client session (one per app_lifespan() call),
    but the login tool's two-call 2FA flow needs the *second* call — which
    may arrive on a different MCP client session than the first — to see the
    background task the first call started. A per-instance field would only
    ever be visible to calls on that same session.
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


async def _startup(browser: BrowserSession, client: OWAClient) -> None:
    """Launch the browser on the persistent profile and make sure it's signed in.

    Runs as a background task instead of inline in app_lifespan(): a cold
    Chromium launch plus a human-paced interactive sign-in takes well past any
    MCP client's connect timeout, so the handshake must complete before any of
    this finishes. Tool calls that hit the browser wait for it lazily
    (BrowserSession ensures its own context is up on first real use).

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
        print(f"[exchange-mcp] Browser profile: {browser.profile_dir} "
              f"({'reusing existing' if browser.profile_existed else 'creating new'})",
              file=sys.stderr, flush=True)
        print("[exchange-mcp] Launching browser...", file=sys.stderr, flush=True)
        await asyncio.to_thread(browser.start)

        if await asyncio.to_thread(browser.has_active_session):
            print("[exchange-mcp] Profile is already authenticated; ready to serve.",
                  file=sys.stderr, flush=True)
            return

        print("[exchange-mcp] Profile is not authenticated - opening a browser window for "
              f"sign-in (waiting up to {LOGIN_WINDOW_SECONDS}s). Please sign in to OWA in that "
              "window, 2FA included.", file=sys.stderr, flush=True)
        result = await asyncio.to_thread(browser.interactive_login, LOGIN_WINDOW_SECONDS)

        if result.get("success"):
            print(f"[exchange-mcp] {result.get('message', 'Signed in successfully.')}",
                  file=sys.stderr, flush=True)
            return

        reason = result.get("reason") or auth_errors.LOGIN_TIMEOUT
        print(f"[exchange-mcp] Sign-in not completed ({reason}): {result.get('error')}",
              file=sys.stderr, flush=True)
        print(f"[exchange-mcp] {auth_errors.remediation(reason)}", file=sys.stderr, flush=True)
        print("[exchange-mcp] The server keeps running; tools will report that authorization "
              "is required until then.", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"[exchange-mcp] Background startup failed: {exc}. "
              "The `login` tool remains available.", file=sys.stderr, flush=True)


_shared_state_lock = asyncio.Lock()
_shared_browser: BrowserSession | None = None
_shared_client: OWAClient | None = None
_shared_startup_task: asyncio.Task | None = None
_shared_pending_login: asyncio.Task | None = None


async def _get_shared_client() -> OWAClient:
    """Create the BrowserSession/OWAClient once, on first use, and reuse it for
    the life of the process.

    Under --transport http, the mcp SDK's StreamableHTTPSessionManager runs a
    fresh low-level Server.run() - and therefore a fresh app_lifespan() call -
    per client session, not once for the whole process. Without this module-level
    singleton, app_lifespan would launch a new browser and log in again on every
    single client connection, then tear it down when that connection closed,
    instead of staying warm and shared as intended.
    """
    global _shared_browser, _shared_client, _shared_startup_task

    async with _shared_state_lock:
        if _shared_client is not None:
            return _shared_client

        owa_url = os.environ.get("EXCHANGE_OWA_URL", "")
        if not owa_url:
            raise ValueError("OWA URL not configured. Set the EXCHANGE_OWA_URL environment variable.")

        profile_dir = os.environ.get("EXCHANGE_BROWSER_PROFILE_DIR") or None
        browser = BrowserSession(owa_url, headless=_resolve_headless(), profile_dir=profile_dir)
        client = OWAClient(browser)
        _shared_startup_task = asyncio.create_task(_startup(browser, client))
        _shared_browser = browser
        _shared_client = client
        return client


@atexit.register
def _stop_shared_browser() -> None:
    if _shared_browser is not None:
        _shared_browser.stop()


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Yield the process-wide shared OWAClient (see _get_shared_client).

    Deliberately does not stop the browser when an individual client session
    ends - only process exit (_stop_shared_browser, above) does that.
    """
    client = await _get_shared_client()
    yield AppContext(client=client)


# Create the MCP server instance
mcp = FastMCP("exchange", lifespan=app_lifespan)

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

# Tools with a known, unfixable server-side bug (see PROJECT_STATUS.md KO rows)
# rather than merely untested or degraded-but-working ones (e.g. get_meeting_contacts,
# which returns an empty result plus a `warnings` field instead of failing). Excluded
# from the MCP tool listing under --stable so a client can't call them and hit a fault.
KNOWN_BUGGY_TOOLS: dict[str, str] = {}


def _apply_stable_mode() -> None:
    """Remove known-buggy tools from the MCP tool listing so --stable clients can't call them."""
    for name, reason in KNOWN_BUGGY_TOOLS.items():
        try:
            mcp.remove_tool(name)
            print(f"[exchange-mcp] --stable: excluded buggy tool '{name}' ({reason})",
                  file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[exchange-mcp] --stable: could not exclude '{name}': {exc}",
                  file=sys.stderr, flush=True)


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

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f"[exchange-mcp] Serving streamable-http on http://{args.host}:{args.port}/mcp",
              file=sys.stderr, flush=True)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
