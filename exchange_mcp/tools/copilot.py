"""Copilot tools for the Exchange MCP server.

Delegates work to Microsoft Copilot's chat pane inside the modern Outlook
web client, via BrowserSession's Copilot UI-automation helpers - unlike
every other tool module here, this drives Copilot's actual DOM instead of
a documented JSON action, because Copilot has no such action. Discovery
capture 20260911-112708-e917 confirmed that directly: no HTTP request in a
full recorded Copilot session carried either the prompt or the generated
answer, so the UI automation is the design, not a temporary shim.

Only usable when the session is in "bearer" auth mode
(BrowserSession.auth_mode); every tool here fails with a clear error
instead of a confusing one on classic OWA tenants.

All five tools pass tests/smoke/tests/test_copilot.py as of 2026-09-11. Four
things had to be true at once, and each was its own live failure:

1. The pane is a **cross-origin iframe** (served from
   _COPILOT_FRAME_HOST_HINTS, not the mailbox host), so a `page.locator(...)`
   can never match inside it - see the comment above
   BrowserSession._async_copilot_frame.
2. That iframe is created and then **replaced** during Copilot's own load, so
   resolving it once returns a corpse - see _async_copilot_open_pane.
3. `pane.inner_text()` is the whole panel, not the answer, and the pane's
   pre-answer chrome is already non-empty *and* already stable - so a naive
   read returned Copilot's UI as a successful answer within seconds. See
   _copilot_answer_text and the `baseline` argument of
   _async_copilot_wait_and_read.
4. **Opening an item does not ground the prompt.** The chat pane is a
   standalone conversation and does not inherit what is on screen; the item's
   text has to be pasted into the prompt. See _ask below.

These remain the most fragile tools in the package - they drive a DOM
Microsoft can restyle at any time, and the smoke test is the only tripwire.
"""

import json

from mcp.server.mcpserver import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import (
    OWAClient,
    BearerModeRequiredError,
    CopilotUnavailableError,
    SessionExpiredError,
)
from exchange_mcp.utils import html_to_text

# How much of an item's body to paste into a prompt. Enough for a real thread,
# short of pasting a 200-message quoted chain into a chat box.
_GROUNDING_BODY_CHARS = 6000


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _fetch_item_text(client: OWAClient, item_id: str) -> tuple[str, str | None]:
    """Subject + plain-text body for an item, to paste into a prompt.

    Returns (text, error). Uses the narrow `IdOnly` + Subject/Body shape rather
    than `AllProperties`, because the wide shape makes OWA's own serialiser
    throw on MeetingRequestMessage items (see PROJECT_STATUS.md) - and grounding
    a Copilot prompt must not be the one thing that fails on a meeting invite.
    This is the same shape get_email_links already reads those items with.
    """
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2017_08_18",
        },
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "IdOnly",
                "BodyType": "HTML",
                "AdditionalProperties": [
                    {"__type": "PropertyUri:#Exchange", "FieldURI": "Subject"},
                    {"__type": "PropertyUri:#Exchange", "FieldURI": "Body"},
                ],
            },
            "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
        },
    }

    try:
        data = client.request("GetItem", payload)
    except SessionExpiredError:
        raise
    except Exception as e:
        return "", f"could not read the item to ground the prompt: {e}"

    for msg in client.extract_items(data):
        if "Items" not in msg:
            continue
        for item in msg["Items"]:
            subject = item.get("Subject", "") or "(no subject)"
            body = html_to_text(item.get("Body", {}).get("Value", "") or "")
            if len(body) > _GROUNDING_BODY_CHARS:
                body = body[:_GROUNDING_BODY_CHARS] + "\n[...truncated...]"
            return f"Subject: {subject}\n\n{body}".strip(), None

    return "", "the item returned no content to ground the prompt with"


def _ask(
    client: OWAClient,
    prompt: str,
    *,
    item_id: str | None,
    item_kind: str,
    timeout: float,
) -> str:
    """Call OWAClient.ask_copilot and translate its exceptions into the
    module's standard JSON error shape, shared by every tool below.

    When `item_id` is given, the item's own subject and body are pasted **into
    the prompt**. Opening the item in the browser first is not grounding: the
    chat pane is a standalone conversation and does not inherit whatever is on
    screen. Confirmed live 2026-09-11 - asked to summarise an open Inbox thread,
    Copilot answered "non vedo alcun thread email allegato o identificato nel tuo
    messaggio" and ran a generic mailbox search instead, and its own follow-up
    chips offered "I'll paste the email thread here". `coach_draft` was the only
    tool producing a useful answer before this, and the reason is that it already
    put its content in the prompt.

    A failed grounding read is reported as `grounding_warning` rather than an
    error: an ungrounded answer is degraded, not worthless, and the caller needs
    to be able to tell which one it got.
    """
    grounding_warning = None
    if item_id:
        try:
            item_text, grounding_warning = _fetch_item_text(client, item_id)
        except SessionExpiredError as e:
            return json.dumps({"error": str(e)})
        if item_text:
            label = "calendar event" if item_kind == "event" else "email thread"
            prompt = (
                f"{prompt}\n\n"
                f"--- begin {label} ---\n{item_text}\n--- end {label} ---"
            )

    try:
        result = client.ask_copilot(prompt, item_id=item_id, item_kind=item_kind, timeout=timeout)
        if isinstance(result, dict):
            result["grounded"] = bool(item_id and not grounding_warning)
            if grounding_warning:
                result["grounding_warning"] = grounding_warning
        return json.dumps(result)
    except BearerModeRequiredError:
        return json.dumps({
            "error": "Copilot requires the modern Outlook backend (bearer auth mode); "
                     "this session is on classic OWA and can't reach it."
        })
    except CopilotUnavailableError as e:
        return json.dumps({"error": f"Copilot is temporarily unavailable: {e}"})
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to reach Copilot: {e}"})


@mcp.tool()
def ask_copilot(
    prompt: str,
    item_id: str = "",
    item_kind: str = "email",
    timeout: float = 90,
    ctx: Context = None,
) -> str:
    """Ask Microsoft Copilot a question via its chat pane in Outlook.

    Generic delegator: sends free-text to Copilot, optionally grounded
    against a specific email or event. Requires the modern Outlook (bearer
    auth) backend - fails clearly if the session is on classic OWA.

    Args:
        prompt: Free-text question or instruction for Copilot.
        item_id: Optional email/event ID to open first, so Copilot can see
            it as context. Leave empty for an ungrounded question.
        item_kind: "email" (default) or "event" - which item item_id refers to.
        timeout: Seconds to wait for a complete response before returning
            a partial result instead of failing.

    Returns:
        JSON: {"status": "ok", "text": ...} on a complete response,
        {"status": "timeout", "partial_text": ...} if Copilot was still
        generating when timeout elapsed, {"status": "no_response", ...} if the
        pane was driven but never changed (carries a content-free
        `pane_structure` for diagnosis), or {"error": ...} on failure.
        Every non-error response also carries `grounded` (whether the item's
        text was pasted into the prompt) and, if that read failed,
        `grounding_warning`.
    """
    client = _get_client(ctx)
    return _ask(client, prompt, item_id=item_id or None, item_kind=item_kind, timeout=timeout)


@mcp.tool()
def summarize_email_thread(item_id: str, timeout: float = 90, ctx: Context = None) -> str:
    """Ask Copilot to summarize an email thread.

    Args:
        item_id: The email's item ID (e.g. from get_emails/search_emails).
        timeout: Seconds to wait for a complete response.

    Returns:
        JSON: {"status": "ok", "text": <summary>} or {"error": ...}.
    """
    client = _get_client(ctx)
    return _ask(
        client, "Summarize this email thread, including any action items.",
        item_id=item_id, item_kind="email", timeout=timeout,
    )


@mcp.tool()
def draft_reply_with_copilot(
    item_id: str, instructions: str, tone: str = "", timeout: float = 90, ctx: Context = None
) -> str:
    """Ask Copilot to draft a reply to an email.

    Returns Copilot's drafted text for the caller to review and pass into
    reply_email(body=...) - it does not send anything itself.

    The chat pane is the right surface for this: the discovery capture
    recorded Outlook's "help me reply" entry point being served from the
    side panel, not from a compose window. That capture never saw a free-text
    input there, only preset prompt chips, which left arbitrary `instructions`
    as an inference - **confirmed live 2026-09-11**: the pane does have a
    composer, and a free-text prompt round-trips through it.

    Args:
        item_id: The email's item ID to reply to.
        instructions: What the reply should say/do (e.g. "accept the meeting
            and ask for the agenda in advance").
        tone: Optional tone hint (e.g. "brief and formal").
        timeout: Seconds to wait for a complete response.

    Returns:
        JSON: {"status": "ok", "text": <drafted reply>} or {"error": ...}.
    """
    client = _get_client(ctx)
    prompt = f"Draft a reply to this email. {instructions}"
    if tone:
        prompt += f" Tone: {tone}."
    return _ask(client, prompt, item_id=item_id, item_kind="email", timeout=timeout)


@mcp.tool()
def coach_draft(item_id: str, draft_text: str, timeout: float = 90, ctx: Context = None) -> str:
    """Ask Copilot's compose coaching for feedback on a draft reply.

    Capture 20260911-112708-e917 exercised every other surface here but turned
    up **no Coaching affordance at all**, which put the whole tool in doubt.
    Settled live on 2026-09-11: none is needed. Coaching is just a prompt, and
    the chat pane answers it like any other - this was in fact the first of the
    five tools to return a useful answer, because it already pasted its content
    (`draft_text`) into the prompt rather than relying on the pane to see the
    item. Whether Outlook *also* has a dedicated coaching surface inside a
    compose window is still unknown, and would be a different tool.

    Args:
        item_id: The email being replied to, for context.
        draft_text: The draft reply text to get coaching feedback on.
        timeout: Seconds to wait for a complete response.

    Returns:
        JSON: {"status": "ok", "text": <coaching feedback>} or {"error": ...}.
    """
    client = _get_client(ctx)
    prompt = f"Give me coaching feedback on this draft reply, focusing on tone and clarity:\n\n{draft_text}"
    return _ask(client, prompt, item_id=item_id, item_kind="email", timeout=timeout)


@mcp.tool()
def meeting_prep(event_id: str, timeout: float = 90, ctx: Context = None) -> str:
    """Ask Copilot to prepare a briefing for an upcoming meeting.

    Args:
        event_id: The calendar event's item ID.
        timeout: Seconds to wait for a complete response.

    Returns:
        JSON: {"status": "ok", "text": <briefing>} or {"error": ...}.
    """
    client = _get_client(ctx)
    return _ask(
        client,
        "Prepare me for this meeting: summarize context, related documents, "
        "and any outstanding action items.",
        item_id=event_id, item_kind="event", timeout=timeout,
    )
