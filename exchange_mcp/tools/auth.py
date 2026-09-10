"""Authentication tool for the Exchange MCP server.

Provides a `login` tool that opens a visible browser window on the OWA sign-in
page and waits for the user to complete it. There are no credentials anywhere in
this server: authentication is either silent (the persistent browser profile is
still signed in) or interactive, driven by the human in front of that window.

The flow is non-blocking, because a real sign-in takes minutes and an MCP call
can't:
1. The first call opens the window and returns immediately.
2. The user signs in (address, password, 2FA) in that window.
3. A second call picks up the result.
"""

import asyncio
import json

from mcp.server.fastmcp import Context

from exchange_mcp import auth_errors
from exchange_mcp.server import mcp, AppContext, LOGIN_WINDOW_SECONDS
from exchange_mcp.owa_client import OWAClient


def _get_app_ctx(ctx: Context) -> AppContext:
    """Extract the AppContext from the MCP lifespan context."""
    return ctx.request_context.lifespan_context


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    return _get_app_ctx(ctx).client


def _session_is_valid(client: OWAClient) -> bool:
    """Quick check: can we reach the inbox?

    Uses FindFolder rather than GetFolder: this OWA deployment's GetFolder
    action returns a flattened, non-EWS response with no ResponseMessages
    envelope, so extract_items() on it is always empty regardless of
    session state.
    """
    try:
        payload = {
            "__type": "FindFolderJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "FindFolderRequest:#Exchange",
                "FolderShape": {
                    "__type": "FolderResponseShape:#Exchange",
                    "BaseShape": "IdOnly",
                },
                "ParentFolderIds": [
                    {
                        "__type": "DistinguishedFolderId:#Exchange",
                        "Id": "inbox",
                    }
                ],
                "Traversal": "Shallow",
                "Paging": {
                    "__type": "IndexedPageView:#Exchange",
                    "BasePoint": "Beginning",
                    "Offset": 0,
                    "MaxEntriesReturned": 1,
                },
            },
        }
        data = client.request("FindFolder", payload)
        items = client.extract_items(data)
        return bool(items)
    except Exception:
        return False


def _failure_payload(result: dict) -> str:
    """Render a failed sign-in, adding the reason's remediation text.

    `reason` comes from the interactive login (see exchange_mcp.auth_errors);
    `authorization_required` is the flag a client should branch on — it means the
    mailbox is unreachable until someone finishes signing in.
    """
    reason = result.get("reason") or auth_errors.LOGIN_TIMEOUT
    return json.dumps({
        "success": False,
        "error": result.get("error") or "Sign-in was not completed.",
        "reason": reason,
        "authorization_required": True,
        "remediation": auth_errors.remediation(reason),
    })


@mcp.tool()
async def login(force: bool = False, ctx: Context = None) -> str:
    """Sign in to Exchange OWA by opening a browser window for the user.

    Call this when a tool reports that authorization is required, or before first
    use. The server holds no credentials: this opens a real, visible browser
    window on the OWA sign-in page, and the *user* signs in there (address,
    password, and any 2FA prompt). The resulting session is saved in the server's
    persistent browser profile and reused from then on, across restarts.

    **Two-call flow**: the first call opens the window and returns immediately —
    tell the user to complete the sign-in in that window. Call `login` again
    afterwards to pick up the result. If the session is already valid, the first
    call says so without opening anything.

    Args:
        force: Open the sign-in window even if the current session looks valid
            (e.g. to switch accounts). Normally leave this off.

    Returns:
        JSON. `{"success": true, ...}` once the session is live. `{"status":
        "awaiting_user_login"}` while the window is open. On failure,
        `"authorization_required": true` plus a `reason` and `remediation`
        describing what the sign-in page was showing when the wait expired.
    """
    app_ctx = _get_app_ctx(ctx)
    client = app_ctx.client

    # ------------------------------------------------------------------
    # If a sign-in window is already open, report on it first
    # ------------------------------------------------------------------
    if app_ctx.pending_login is not None:
        task = app_ctx.pending_login

        if not task.done():
            return json.dumps({
                "status": "awaiting_user_login",
                "message": "The browser window is still open and waiting. Ask the user to "
                           "complete the OWA sign-in there (including 2FA), then call login again.",
            })

        # Task finished — harvest result and clear
        app_ctx.pending_login = None
        try:
            result = task.result()
        except Exception as e:
            return json.dumps({"success": False, "error": f"Interactive login failed: {e}"})

        if result.get("success") and _session_is_valid(client):
            return json.dumps({
                "success": True,
                "message": "Signed in and session verified.",
                "browser_shown": result.get("browser_shown", False),
            })
        return _failure_payload(result)

    # ------------------------------------------------------------------
    # No window open — normal flow
    # ------------------------------------------------------------------
    if not force and _session_is_valid(client):
        return json.dumps({"success": True, "message": "Session is already active."})

    # Started as a background task, not awaited: interactive_login() blocks for
    # minutes waiting on a human, far longer than any MCP client will hold a
    # request open. asyncio.to_thread keeps the server's own event loop free
    # while that wait happens on a worker thread.
    app_ctx.pending_login = asyncio.create_task(
        asyncio.to_thread(client.browser.interactive_login, LOGIN_WINDOW_SECONDS)
    )

    return json.dumps({
        "status": "awaiting_user_login",
        "message": "A browser window has been opened on the OWA sign-in page. Ask the user to "
                   f"sign in there (including any 2FA prompt) within {LOGIN_WINDOW_SECONDS} "
                   "seconds, then call login again to confirm.",
    })
