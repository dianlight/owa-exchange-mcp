"""Exchange MCP Server.

Exposes OWA email, calendar, directory, and availability tools via MCP.
Uses FastMCP with a lifespan context manager to share a single OWAClient
(backed by one persistent browser session) across all tool invocations.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

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


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Launch the persistent browser and, if possible, log in before serving."""
    owa_url = os.environ.get("EXCHANGE_OWA_URL", "")
    if not owa_url:
        raise ValueError("OWA URL not configured. Set the EXCHANGE_OWA_URL environment variable.")

    profile_dir = os.environ.get("EXCHANGE_BROWSER_PROFILE_DIR") or None
    browser = BrowserSession(owa_url, headless=_resolve_headless(), profile_dir=profile_dir)
    print("[exchange-mcp] Launching browser...", file=sys.stderr, flush=True)
    browser.start()

    client = OWAClient(browser)

    master_password = os.environ.get("EXCHANGE_MASTER_PASSWORD")
    if master_password:
        from login import CREDS_FILE, decrypt_credentials

        if CREDS_FILE.exists():
            username, password = decrypt_credentials(master_password)
            if username:
                client.user_email = username
                print(f"[exchange-mcp] Logging in as {username} (waiting for 2FA if prompted)...", file=sys.stderr, flush=True)
                result = browser.ensure_logged_in(username, password)
                if result.get("success"):
                    print("[exchange-mcp] Login successful.", file=sys.stderr, flush=True)
                else:
                    print(f"[exchange-mcp] Login failed: {result.get('error')}. "
                          "The `login` tool remains available.", file=sys.stderr, flush=True)
            else:
                print("[exchange-mcp] EXCHANGE_MASTER_PASSWORD set but could not decrypt credentials.",
                      file=sys.stderr, flush=True)
        else:
            print("[exchange-mcp] EXCHANGE_MASTER_PASSWORD set but no stored credentials found "
                  "(run login.py --setup).", file=sys.stderr, flush=True)
    else:
        print("[exchange-mcp] No EXCHANGE_MASTER_PASSWORD set; starting without a session. "
              "Use the `login` tool to authenticate.", file=sys.stderr, flush=True)

    try:
        yield AppContext(client=client)
    finally:
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
    """Entry point: run the MCP server over stdio."""
    parser = argparse.ArgumentParser(description="Exchange MCP server")
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Run the browser with a visible window instead of headless (for debugging).",
    )
    parser.parse_args()

    mcp.run()


if __name__ == "__main__":
    main()
