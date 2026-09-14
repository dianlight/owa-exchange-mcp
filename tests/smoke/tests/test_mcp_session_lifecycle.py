"""Smoke test: MCP transport-session lifecycle behind `check_session` (issue #18).

Issue #18 reported `check_session` failing with a JSONRPC error, "Session not
found", while `get_tasks` "worked perfectly" against the same server. The name
invites the obvious reading -- that the OWA session is broken -- and that reading
is wrong twice over:

- "Session not found" is not this codebase's text. It is emitted verbatim by
  `mcp/server/streamable_http_manager.py` as a **404** whenever a request carries
  an `Mcp-Session-Id` the manager doesn't hold in `_server_instances`: never
  issued, already terminated, or cleaned up after that session's task crashed.
  The server runs stateful streamable-HTTP (`stateless_http` is left at its
  default `False`), so a session id is mandatory on every post-`initialize` call.
- That lookup happens in the manager, *before* the request is dispatched to any
  tool. So the 404 cannot depend on which tool was named -- which is exactly what
  this test pins down: with a dead session id, `check_session` and `get_tasks`
  fail *identically*. A tool-specific bug cannot produce a tool-independent error.

Which leaves the real cause: the two calls rode different transport sessions. A
client holding an id the server no longer knows gets the 404 on whatever it calls
next, then typically re-initializes transparently -- so the *following* call
succeeds and looks like proof that only the first tool is broken.

Running this test live narrowed that further, and the narrowing is the reason the
phases below assert *specific* wording rather than "a 404". The SDK has three
distinct dead-session texts (tabulated at the constants), and #18's "Session not
found" is only ever the manager's "this id is not in `_server_instances`". A client
that had cleanly terminated its own session gets "Not Found: Session has been
terminated" instead -- verified here. So #18's wording rules the tidy-client case
out and points at the session *instance* being gone: the server restarted between
the two calls, or that session's task crashed and was swept.

So this test asserts three things, in order of what #18 actually needs:
1. `check_session` works, repeatedly and after an idle gap, on one live session
   (the symptom, as a regression guard -- a session must not die under its own
   client mid-life).
2. A dead session id yields the same 404 "Session not found" for `check_session`
   and for `get_tasks` (the discriminator that clears the tool).
3. A fresh `initialize` recovers (the remediation a client should apply).

Read-only: `check_session` is a `FindFolder` on the inbox and `get_tasks` a
`FindItem`, so no mailbox state is created or changed and the test is repeatable.

Run standalone:
    python -m tests.smoke.tests.test_mcp_session_lifecycle
"""

import asyncio
import json
import sys
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.smoke.mcp_client import call, run, session
from tests.smoke.results import is_error_payload, record
from tests.smoke.server_manager import SERVER_URL, ensure_server

# Long enough to catch a session reaped out from under an idle client (the #18
# symptom), short enough not to dominate the run.
IDLE_GAP_SECONDS = 20

# A dead session id can 404 with three *different* texts, and the difference is
# the diagnosis -- confirmed live 2026-09-14 (see the docstring above):
#
#   "Session not found"                       manager: id not in _server_instances
#                                             at all -- never issued, or the
#                                             session crashed and was cleaned up.
#                                             **Issue #18's exact wording.**
#   "Not Found: Session has been terminated"  transport: still registered, but the
#                                             client cleanly DELETEd it.
#   "Not Found: Invalid or expired session ID" mismatched id on an `initialize`.
#
# So #18 cannot have been a client that tidied up after itself: an unknown id
# means the *instance was gone*, which on a healthy server means it restarted.
DEAD_SESSION_STATUS = 404
UNKNOWN_SESSION_MESSAGE = "Session not found"
TERMINATED_SESSION_MESSAGE = "Not Found: Session has been terminated"


def _raw_tool_call(session_id: str, tool: str) -> tuple[int, str]:
    """POST a tools/call bypassing ClientSession, with an arbitrary session id.

    The SDK client can't express "use an id the server doesn't know" -- it only
    ever sends one the server handed it. Reproducing #18 means forging that
    header, so this goes out over plain httpx. `follow_redirects` because the
    streamable-HTTP app is mounted at /mcp and Starlette may redirect to /mcp/.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }
    response = httpx.post(
        SERVER_URL,
        json=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id,
        },
        timeout=30,
        follow_redirects=True,
    )
    return response.status_code, response.text


def _dead_session_message(status: int, text: str) -> str | None:
    """The JSONRPC error message of a dead-session 404, or None if it isn't one.

    Returning the message rather than a bool is what lets a caller tell the three
    causes above apart instead of lumping them into "some kind of 404".
    """
    if status != DEAD_SESSION_STATUS:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    message = payload.get("error", {}).get("message")
    return message if isinstance(message, str) else None


async def _live_session_holds_up() -> tuple[bool, str | None]:
    """Phase 1: check_session works twice plus once after an idle gap.

    Returns (ok, session_id). The id is captured for phase 2: once this context
    exits, the client terminates the session, so that id becomes exactly the
    stale id a #18 client is holding.
    """
    ensure_server()
    async with streamablehttp_client(SERVER_URL) as (read, write, get_session_id):
        async with ClientSession(read, write) as s:
            await s.initialize()
            session_id = get_session_id()

            for phase, delay in (("first call", 0),
                                 ("immediate repeat", 0),
                                 (f"after {IDLE_GAP_SECONDS}s idle", IDLE_GAP_SECONDS)):
                if delay:
                    await asyncio.sleep(delay)

                args = {"_phase": phase}
                info = await call(s, "check_session")

                err = is_error_payload(info)
                if err:
                    record("check_session", args, "EXCEPTION", f"{phase}: {err}")
                    return False, session_id

                if not isinstance(info, dict) or "authenticated" not in info:
                    record("check_session", args, "EXCEPTION",
                           f"{phase}: no 'authenticated' key in {str(info)[:120]}")
                    return False, session_id

                if not info.get("authenticated"):
                    # Not a #18 failure -- the profile genuinely isn't signed in.
                    # test_check_session.py owns that path (it polls for a
                    # startup login); here it just means the run can't conclude.
                    record("check_session", args, "TOOL_ERROR",
                           f"{phase}: not authenticated ({info.get('error', 'no reason given')}) "
                           "-- sign in and re-run; this test needs a live session")
                    return False, session_id

                record("check_session", args, "OK",
                       f"{phase}: authenticated on session {session_id}")

            return True, session_id


def _dead_id_is_tool_independent(session_id: str, label: str, expected: str) -> bool:
    """Phase 2: the same dead id must fail identically for both tools.

    This is the assertion that clears `check_session`. Two ways it can fail, and
    they mean opposite things:

    - The two tools disagree -> the premise of #18 was right after all, the 404 is
      somehow tool-dependent, and this needs investigating rather than closing.
    - Both agree but on the wrong message -> the SDK changed which of the three
      dead-session texts it uses, so #18's wording no longer identifies its cause
      and the table of causes above is stale.
    """
    messages = {}
    args = {"_phase": f"{label} session id", "_raw_post": True}

    for tool in ("check_session", "get_tasks"):
        status, text = _raw_tool_call(session_id, tool)
        message = _dead_session_message(status, text)
        messages[tool] = message
        if message == expected:
            record(tool, args, "OK",
                   f'HTTP {status} "{message}" -- transport-level, as expected')
        else:
            record(tool, args, "EXCEPTION",
                   f'expected {DEAD_SESSION_STATUS} "{expected}", '
                   f"got HTTP {status}: {text[:160]}")

    if messages["check_session"] != messages["get_tasks"]:
        record("check_session", args, "EXCEPTION",
               "TOOL-DEPENDENT transport failure: check_session got "
               f"{messages['check_session']!r} vs get_tasks {messages['get_tasks']!r} "
               "-- issue #18 would be a real check_session bug, investigate")
        return False

    return all(m == expected for m in messages.values())


async def _fresh_session_recovers() -> bool:
    """Phase 3: re-initializing after the 404 restores service."""
    async with session() as s:
        info = await call(s, "check_session")
        args = {"_phase": "reconnect after dead session"}

        err = is_error_payload(info)
        if err:
            record("check_session", args, "EXCEPTION", f"reconnect failed: {err}")
            return False
        if not (isinstance(info, dict) and info.get("authenticated")):
            record("check_session", args, "TOOL_ERROR",
                   f"reconnect returned {str(info)[:160]}")
            return False

        record("check_session", args, "OK",
               "fresh initialize recovers -- the correct client response to a 404")
        return True


async def main() -> bool:
    live_ok, session_id = await _live_session_holds_up()
    if not live_ok:
        return False

    # Phase 1's context has exited, so the client cleanly terminated `session_id`
    # on the way out -- the tidy-client case, which the SDK words differently.
    if not _dead_id_is_tool_independent(session_id, "terminated",
                                       TERMINATED_SESSION_MESSAGE):
        return False

    # An id the server never issued: the restarted-server / crashed-session case,
    # and the one whose wording issue #18 actually reported.
    if not _dead_id_is_tool_independent(uuid4().hex, "never-issued",
                                        UNKNOWN_SESSION_MESSAGE):
        return False

    return await _fresh_session_recovers()


if __name__ == "__main__":
    ok = run(main())
    sys.exit(0 if ok else 1)
