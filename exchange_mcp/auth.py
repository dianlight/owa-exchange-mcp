"""Shared login logic for Exchange MCP.

Reuses credential-encryption helpers from login.py. The actual login now
runs against the MCP server's shared, persistent BrowserSession (see
browser_session.py) instead of a throwaway browser, so a successful login
lives in the same profile the server keeps reusing for every OWA call.
"""

import asyncio
import sys
from pathlib import Path

# Ensure login.py (project root) is importable regardless of cwd
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from login import CREDS_FILE, SALT_FILE, get_key, encrypt_credentials, decrypt_credentials  # noqa: F401


async def perform_login(browser_session, username: str, password: str) -> dict:
    """Authenticate to OWA via the shared persistent BrowserSession.

    ensure_logged_in() blocks its calling thread (page navigation, and
    possibly a 90s wait for mobile 2FA approval), so it runs in a worker
    thread via run_in_executor -- otherwise it would stall the MCP server's
    own event loop while the login tool polls the resulting task.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, browser_session.ensure_logged_in, username, password)
