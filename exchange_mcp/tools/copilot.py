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

The pane is a **cross-origin iframe**, which is what made every tool here
fail its first live run - see the comment block above
BrowserSession._async_copilot_frame, and PROJECT_STATUS.md's Copilot notes
for what that capture did and didn't settle. These tools are still marked
KO pending a re-run of tests/smoke/tests/test_copilot.py.
"""

import json

from mcp.server.fastmcp import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import (
    OWAClient,
    BearerModeRequiredError,
    CopilotUnavailableError,
    SessionExpiredError,
)


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _ask(client: OWAClient, prompt: str, *, item_id: str | None, item_kind: str, timeout: float) -> str:
    """Call OWAClient.ask_copilot and translate its exceptions into the
    module's standard JSON error shape, shared by every tool below."""
    try:
        result = client.ask_copilot(prompt, item_id=item_id, item_kind=item_kind, timeout=timeout)
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
        generating when timeout elapsed, or {"error": ...} on failure.
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
    side panel, not from a compose window. What it did *not* see is a
    free-text input in that panel - only preset prompt chips - so passing
    arbitrary `instructions` through is still an inference.

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

    The least-evidenced tool in this module. A recorded live Copilot session
    (capture 20260911-112708-e917) exercised every other surface here but
    turned up **no Coaching affordance at all**, so it's unknown whether the
    feature exists on this tenant, let alone whether it lives in the chat
    pane this tool drives or inside an in-progress compose window. Treat
    results as provisional until PROJECT_STATUS.md marks it verified.

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
