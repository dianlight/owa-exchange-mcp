"""Authentication tool for the Exchange MCP server.

Provides a `login` tool that handles credential setup and 2FA login against
the server's shared, persistent browser session (see browser_session.py).

The login flow is non-blocking for 2FA:
1. First call starts browser login in the background and returns immediately
   with instructions to approve 2FA on the mobile app.
2. Second call checks the background task result.
"""

import asyncio
import json

from mcp.server.fastmcp import Context

from exchange_mcp.server import mcp, AppContext
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


@mcp.tool()
async def login(
    master_password: str,
    username: str = "",
    password: str = "",
    ctx: Context = None,
) -> str:
    """Authenticate to Exchange OWA (handles credential setup and 2FA login).

    Call this tool when the session has expired or before first use. Login
    runs on the MCP server's shared persistent browser session, with mobile
    push 2FA approval.

    **Two-call 2FA flow**: The first call starts the browser login in the
    background and returns immediately asking you to tell the user to approve
    2FA on their phone.  Call login again with the same master_password after
    the user approves — the second call picks up the result.

    Args:
        master_password: Decrypts stored credentials, or encrypts new ones
            if username/password are also provided.
        username: Email address. Provide together with password for first-time
            credential setup (replaces `login.py --setup`).
        password: Account password. Required together with username for setup.

    Returns:
        JSON result with success status and any error details.
    """
    app_ctx = _get_app_ctx(ctx)
    client = app_ctx.client

    # ------------------------------------------------------------------
    # If a background login task exists, check its status first
    # ------------------------------------------------------------------
    if app_ctx.pending_login is not None:
        task = app_ctx.pending_login

        if not task.done():
            return json.dumps({
                "status": "awaiting_2fa",
                "message": "Still waiting for 2FA approval. Please approve the login in your authenticator app, then call login again.",
            })

        # Task finished — harvest result and clear
        app_ctx.pending_login = None
        try:
            result = task.result()
        except Exception as e:
            return json.dumps({"success": False, "error": f"Background login failed: {e}"})

        if result.get("success") and _session_is_valid(client):
            return json.dumps({"success": True, "message": "Logged in and session verified."})
        return json.dumps(result)

    # ------------------------------------------------------------------
    # No pending task — normal login flow
    # ------------------------------------------------------------------

    # 1. Check if already authenticated
    if _session_is_valid(client):
        return json.dumps({"success": True, "message": "Session is already active."})

    # 2. Resolve credentials
    from exchange_mcp.auth import encrypt_credentials, decrypt_credentials, CREDS_FILE, perform_login

    if username and password:
        # First-time setup: encrypt and save credentials
        encrypt_credentials(username, password, master_password)
    else:
        # Decrypt existing credentials
        if not CREDS_FILE.exists():
            return json.dumps({
                "success": False,
                "error": "No stored credentials found. Provide username and password for first-time setup.",
            })
        username, password = decrypt_credentials(master_password)
        if not username:
            return json.dumps({
                "success": False,
                "error": "Invalid master password — could not decrypt credentials.",
            })

    # Store user email on the client for availability queries
    client.user_email = username

    # 3. Start browser login in background (non-blocking for 2FA)
    app_ctx.pending_login = asyncio.create_task(
        perform_login(client.browser, username, password)
    )

    return json.dumps({
        "status": "awaiting_2fa",
        "message": "Please approve the login in your 2FA authenticator app, then call login again with the same master_password.",
    })
