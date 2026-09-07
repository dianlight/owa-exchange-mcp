"""Exchange MCP Server.

Exposes OWA email, calendar, directory, and availability tools via MCP.
Uses FastMCP with a lifespan context manager to share a single OWAClient
(backed by one persistent browser session) across all tool invocations.
"""

import argparse
import asyncio
import os
import sys
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning

# mcp's FastMCP.Settings has a `lifespan` field typed with a self-referential
# generic (Callable[[FastMCP[LifespanResultT]], ...]); pydantic-settings can't
# resolve it and warns on every FastMCP(...) construction. That field is never
# read from an env var, so the warning doesn't apply here — silence it.
warnings.filterwarnings("ignore", category=IncompleteFieldDefinitionWarning)

from mcp.server.fastmcp import FastMCP

from exchange_mcp.browser_session import BrowserSession
from exchange_mcp.owa_client import OWAClient


@dataclass
class AppContext:
    """Shared application state available to all tools via lifespan context."""
    client: OWAClient
    pending_login: asyncio.Task | None = field(default=None, repr=False)


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
    `export` to inherit from) pick up EXCHANGE_OWA_URL / EXCHANGE_MASTER_PASSWORD
    the same way an MCP client's stdio `env` block does today.
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


async def _startup(browser: BrowserSession, client: OWAClient) -> None:
    """Launch the browser and, if configured, log in — off the MCP handshake path.

    Runs as a background task instead of inline in app_lifespan(): a cold
    Chromium launch plus an interactive 2FA wait can take well past the
    MCP client's connect timeout, so the handshake must complete before
    any of this finishes. Tool calls that hit the browser wait for it
    lazily (BrowserSession ensures its own context is up on first real use).
    """
    try:
        print("[exchange-mcp] Launching browser...", file=sys.stderr, flush=True)
        await asyncio.to_thread(browser.start)

        master_password = os.environ.get("EXCHANGE_MASTER_PASSWORD")
        if not master_password:
            print("[exchange-mcp] No EXCHANGE_MASTER_PASSWORD set; starting without a session. "
                  "Use the `login` tool to authenticate.", file=sys.stderr, flush=True)
            return

        from exchange_mcp.auth import CREDS_FILE, decrypt_credentials

        if not CREDS_FILE.exists():
            print("[exchange-mcp] EXCHANGE_MASTER_PASSWORD set but no stored credentials found "
                  "(run login.py --setup).", file=sys.stderr, flush=True)
            return

        username, password = decrypt_credentials(master_password)
        if not username:
            print("[exchange-mcp] EXCHANGE_MASTER_PASSWORD set but could not decrypt credentials.",
                  file=sys.stderr, flush=True)
            return

        client.user_email = username
        print(f"[exchange-mcp] Logging in as {username} (waiting for 2FA if prompted)...", file=sys.stderr, flush=True)
        result = await asyncio.to_thread(browser.ensure_logged_in, username, password)
        if result.get("success"):
            print("[exchange-mcp] Login successful.", file=sys.stderr, flush=True)
        else:
            print(f"[exchange-mcp] Login failed: {result.get('error')}. "
                  "The `login` tool remains available.", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"[exchange-mcp] Background startup failed: {exc}. "
              "The `login` tool remains available.", file=sys.stderr, flush=True)


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Set up the shared OWAClient and kick off browser startup in the background."""
    owa_url = os.environ.get("EXCHANGE_OWA_URL", "")
    if not owa_url:
        raise ValueError("OWA URL not configured. Set the EXCHANGE_OWA_URL environment variable.")

    profile_dir = os.environ.get("EXCHANGE_BROWSER_PROFILE_DIR") or None
    browser = BrowserSession(owa_url, headless=_resolve_headless(), profile_dir=profile_dir)
    client = OWAClient(browser)
    startup_task = asyncio.create_task(_startup(browser, client))

    try:
        yield AppContext(client=client)
    finally:
        startup_task.cancel()
        browser.stop()


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
    args = parser.parse_args()

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
