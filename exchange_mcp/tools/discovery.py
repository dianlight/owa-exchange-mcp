"""Capability-discovery tools — record a live OWA session and classify its API surface.

Module **11** in PROJECT_STATUS.md. Unlike every other tool module here,
these don't read or write a mailbox: they exist to find out **what OWA can do
that this server can't yet**, and to turn that into concrete implementation
proposals. The intended driver is the `owa-capability-discovery` skill
(`.claude/skills/owa-capability-discovery/SKILL.md`), which walks a user
through declaring a scope, driving the browser, and reviewing the result.

The split of responsibilities is deliberate:

- [discovery_session.py](../discovery_session.py) — capture. Its own Chromium
  on a throwaway profile, visible, driven by the user. Never touches the
  shared `BrowserSession`.
- [capability_inventory.py](../capability_inventory.py) — the baseline, read
  by `ast` out of this repo's own source so it can't go stale.
- [capability_classify.py](../capability_classify.py) — the verdicts and the
  proposals. Pure logic, unit-tested.
- this module — the MCP surface over those three.

**Start-and-poll, not block.** `start_discovery_session` returns as soon as
the window is up, exactly like the `login` tool: a discovery session includes
a human sign-in plus however long the user spends clicking around, which is
minutes to tens of minutes, and no MCP client holds a request open that long.
The caller polls `get_discovery_status` until `state` leaves `recording` —
which happens on its own when the user closes the browser window.

**Classification reads the files, not the recorder.** So a capture survives a
server restart, and so `classify_discovery_session` can be re-run with a
different `scope` (or after the inventory changes, e.g. once a proposal has
been implemented) without re-recording anything.
"""

import json

from mcp.server.fastmcp import Context

from exchange_mcp import discovery_session as ds
from exchange_mcp.capability_classify import classify, render_markdown
from exchange_mcp.capability_inventory import build_inventory
from exchange_mcp.owa_client import OWAClient
from exchange_mcp.server import mcp, AppContext

# How many findings the tool response carries inline. The full set always
# lands in report.json - this only bounds what an LLM has to read.
_DEFAULT_MAX_FINDINGS = 25

# Keys dropped from a finding before it goes into the tool response. The
# sample payload and response skeleton are the bulky part and are exactly what
# `get_discovery_detail` exists to fetch on demand.
_BULKY_FINDING_KEYS = ("sample_request", "response_shape")


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _slim(finding: dict) -> dict:
    return {k: v for k, v in finding.items() if k not in _BULKY_FINDING_KEYS}


@mcp.tool()
def start_discovery_session(
    scope: str,
    notes: str = "",
    start_url: str | None = None,
    capture_response_bodies: bool = False,
    keep_profile: bool = False,
    ctx: Context = None,
) -> str:
    """Open a fresh, independent OWA browser window and record everything the user does.

    Opens a **new Chromium instance on a brand-new temporary profile** — not
    the server's own signed-in profile — so the capture is clean and nothing
    done in it can affect the profile the other tools serve from. Because the
    profile is empty, **the user must sign in inside that window**; sign-in
    traffic is dropped from the capture entirely and no credential, cookie or
    bearer token is ever written to disk.

    Recording continues until the user closes the browser window (or
    `stop_discovery_session` is called). This call returns immediately — poll
    `get_discovery_status` to see when it has finished, then
    `classify_discovery_session` to get the analysis.

    Args:
        scope: What the user wants to explore, in their own words (e.g.
            "inbox rules and automatic replies", "sharing a calendar with a
            colleague"). Used to decide which observations are relevant and
            to name proposed capability classes — worth being specific.
        notes: Optional free text recorded in the session manifest (a ticket
            reference, what to try, anything the report should carry).
        start_url: Where to open the window. Defaults to the configured OWA
            root. Point it straight at a settings page to save clicks.
        capture_response_bodies: Also store full response bodies. Off by
            default: only a content-free *shape* (field names and types) is
            recorded, which is enough to write a response mapping and keeps
            mailbox content out of the capture files.
        keep_profile: Keep the temporary browser profile after the session
            instead of deleting it. Only useful for debugging the recorder.

    Returns:
        JSON with `session_id`, the capture directory, and instructions to
        relay to the user. On refusal, `error` plus the `session_id` of the
        recording already in progress.
    """
    try:
        active = ds.active_recorders()
        if active:
            return json.dumps({
                "error": "A discovery session is already recording. Finish it (close its "
                         "browser window, or call stop_discovery_session) before starting another.",
                "session_id": active[0].session_id,
                "scope": active[0].scope,
            })

        if not (scope or "").strip():
            return json.dumps({
                "error": "A scope is required — it decides which observations count as "
                         "relevant and what a new capability class gets called. Ask the user "
                         "what they want to explore in OWA."
            })

        client = _get_client(ctx)
        recorder = ds.DiscoveryRecorder(
            client.browser.owa_url,
            scope=scope,
            notes=notes,
            start_url=start_url,
            capture_response_bodies=capture_response_bodies,
            keep_profile=keep_profile,
        )
        ds.register(recorder)
        status = recorder.start()

        return json.dumps({
            "success": True,
            "session_id": recorder.session_id,
            "state": status["state"],
            "session_dir": status["session_dir"],
            "scope": scope,
            "user_instructions": [
                "A new browser window has opened on a fresh, empty profile.",
                "Sign in there — the profile is new, so no existing session is reused. "
                "Sign-in traffic is not recorded.",
                "Then do in OWA exactly what the declared scope describes: open the "
                "settings pages, run the actions, click the buttons.",
                "Close the browser window when finished — that ends the recording.",
            ],
            "next_step": "Poll get_discovery_status until state is 'finished', then call "
                         "classify_discovery_session.",
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Failed to start a discovery session: {e}"})


@mcp.tool()
def get_discovery_status(session_id: str | None = None, ctx: Context = None) -> str:
    """Check on a discovery recording: is it still going, and what has it captured?

    Args:
        session_id: Which session. Omit for the most recent one in this
            server process.

    Returns:
        JSON with `state` (`recording` / `finished` / `stopped`), live capture
        counters (`api_calls`, `ui_actions`, `navigations`, plus how much was
        filtered as noise or dropped as sign-in traffic), and the capture
        directory. `state: "finished"` means the user closed the window and
        the capture is ready to classify.
    """
    try:
        recorder = ds.get_recorder(session_id)
        if recorder is not None:
            status = recorder.status()
            status["success"] = True
            if status["state"] == "recording":
                status["hint"] = ("Still recording. Ask the user to keep exercising the scope "
                                  "in that window, and to close it when done.")
            elif not status["counters"]["api_calls"]:
                status["hint"] = ("No API calls were captured. The most likely cause is that "
                                  "the sign-in was never completed in that window.")
            else:
                status["hint"] = "Capture complete — call classify_discovery_session."
            return json.dumps(status, ensure_ascii=False)

        # Not live in this process: it may still be on disk from before a restart.
        if session_id:
            loaded = ds.load_session(session_id)
            if loaded["exists"]:
                return json.dumps({
                    "success": True,
                    "session_id": session_id,
                    "state": loaded["manifest"].get("state", "unknown"),
                    "session_dir": loaded["session_dir"],
                    "counters": loaded["manifest"].get("counters", {}),
                    "note": "Loaded from disk — this session's recorder is not live in the "
                            "current server process, but the capture can still be classified.",
                }, ensure_ascii=False)

        return json.dumps({
            "error": "No discovery session found. Call start_discovery_session first, "
                     "or list_discovery_sessions to see past captures.",
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to read discovery status: {e}"})


@mcp.tool()
def stop_discovery_session(session_id: str | None = None, ctx: Context = None) -> str:
    """End a discovery recording now, closing its browser window.

    Normally unnecessary — the user closing the window ends the recording by
    itself. Use this when the window is unreachable, or to stop a session the
    user has walked away from.

    Args:
        session_id: Which session. Omit for the most recent one.

    Returns:
        JSON with the final state and capture counters.
    """
    try:
        recorder = ds.get_recorder(session_id)
        if recorder is None:
            return json.dumps({"error": "No live discovery session to stop."})
        status = recorder.stop()
        status["success"] = True
        status["next_step"] = "Call classify_discovery_session to analyse the capture."
        return json.dumps(status, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Failed to stop the discovery session: {e}"})


@mcp.tool()
def classify_discovery_session(
    session_id: str | None = None,
    scope: str | None = None,
    max_findings: int = _DEFAULT_MAX_FINDINGS,
    ctx: Context = None,
) -> str:
    """Classify a recorded session's API calls and propose MCP implementations.

    Compares every distinct endpoint the capture saw against what this server
    already implements — a baseline read directly out of this repo's source,
    so it is never stale — and sorts each one into:

    - **`unknown_api`** — no tool here calls it → a new tool.
    - **`known_api_new_parameters`** — a tool calls it, but OWA sent request
      fields we never send → extend that tool.
    - **`known_api_covered`** — already fully exercised.

    Endpoints are also grouped into capability classes; a cluster that maps to
    no existing tool module becomes a proposed **new module**, with a module
    number and permanent tool IDs allocated per CLAUDE.md's numbering rule.

    Verdicts are biased toward under-reporting (a missed finding beats a
    fabricated one) and rest partly on keyword tables — every finding carries
    its evidence so a wrong label can be overruled.

    Args:
        session_id: Which capture to analyse. Omit for the most recent.
        scope: Override the scope declared at recording time. Useful for
            re-reading one capture from a different angle — nothing is
            re-recorded.
        max_findings: How many findings to inline in this response (default
            25). The complete set is always written to `report.json`.

    Returns:
        JSON with `counts`, `proposals` (`new_modules` / `new_tools` /
        `extend_tools`), the top findings, and the paths of the written
        `report.json` and `report.md`.
    """
    try:
        recorder = ds.get_recorder(session_id)
        resolved_id = session_id or (recorder.session_id if recorder else None)
        if not resolved_id:
            return json.dumps({
                "error": "No discovery session specified and none recorded in this process. "
                         "Pass session_id, or see list_discovery_sessions.",
            })

        if recorder is not None and recorder.state == "recording":
            return json.dumps({
                "error": "That session is still recording. Ask the user to close the browser "
                         "window (or call stop_discovery_session) first.",
                "session_id": resolved_id,
            })

        loaded = ds.load_session(resolved_id)
        if not loaded["exists"]:
            return json.dumps({"error": f"No capture found for session '{resolved_id}'."})
        if not loaded["network"]:
            return json.dumps({
                "error": "That capture holds no API calls, so there is nothing to classify.",
                "session_id": resolved_id,
                "likely_cause": "The sign-in was probably never completed in the recording window.",
            })

        effective_scope = scope if scope is not None else loaded["manifest"].get("scope", "")
        report = classify(
            loaded["network"],
            build_inventory(),
            scope=effective_scope,
            session_id=resolved_id,
            ui_actions=loaded["ui_actions"],
        )

        directory = ds.session_dir_for(resolved_id)
        report_json = directory / "report.json"
        report_md = directory / "report.md"
        report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report_md.write_text(render_markdown(report), encoding="utf-8")

        findings = [_slim(f) for f in report["findings"][:max(1, max_findings)]]
        return json.dumps({
            "success": True,
            "session_id": resolved_id,
            "scope": effective_scope,
            "counts": report["counts"],
            "baseline": report["baseline"],
            "proposals": report["proposals"],
            "findings": findings,
            "findings_truncated": len(report["findings"]) - len(findings),
            "report_json": str(report_json),
            "report_markdown": str(report_md),
            "next_step": "Show the user report.md, then use get_discovery_detail on any "
                         "endpoint you are about to implement to see its real payload.",
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Failed to classify the discovery session: {e}"})


@mcp.tool()
def list_discovery_sessions(ctx: Context = None) -> str:
    """List recorded discovery sessions, newest first.

    Returns:
        JSON array of sessions with their `session_id`, `state`, declared
        `scope`, capture counters, and whether a report has been generated.
        Includes captures from previous server runs — classification works
        off the files, so any of them can still be analysed.
    """
    try:
        sessions = ds.list_sessions()
        return json.dumps({
            "success": True,
            "count": len(sessions),
            "sessions_dir": str(ds.default_discovery_dir()),
            "sessions": sessions,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Failed to list discovery sessions: {e}"})


@mcp.tool()
def get_discovery_detail(
    endpoint: str,
    session_id: str | None = None,
    limit: int = 3,
    include_response_shape: bool = True,
    ctx: Context = None,
) -> str:
    """Get the real captured requests for one endpoint, to implement against.

    What you call once a proposal has been chosen: the classification report
    says *that* an endpoint is worth implementing, this returns the actual
    wire traffic — full request payload, the `__type` annotations,
    `RequestServerVersion`, whether the payload rode in the
    `X-OWA-UrlPostData` header, and the response's field/type skeleton.

    Args:
        endpoint: EWS action name (e.g. `GetInboxRules`) or a URL path
            fragment (e.g. `/PeopleGraphVx/`). Matched case-insensitively
            against both the action and the path.
        session_id: Which capture. Omit for the most recent.
        limit: How many matching calls to return (default 3). More is useful
            when the same action is used for several different operations.
        include_response_shape: Include the response's field/type skeleton.
            Turn off for a smaller response.

    Returns:
        JSON with the matching calls, each carrying `method`, `url`, `action`,
        `status`, recorded headers, `header_payload`, the full request `body`,
        and (optionally) `response_shape`.
    """
    try:
        recorder = ds.get_recorder(session_id)
        resolved_id = session_id or (recorder.session_id if recorder else None)
        if not resolved_id:
            return json.dumps({"error": "No discovery session specified and none in this process."})

        loaded = ds.load_session(resolved_id)
        if not loaded["exists"]:
            return json.dumps({"error": f"No capture found for session '{resolved_id}'."})

        needle = (endpoint or "").strip().lower()
        if not needle:
            return json.dumps({"error": "Pass an endpoint: an EWS action name or a path fragment."})

        matches = []
        for record in loaded["network"]:
            if record.get("type") != "request":
                continue
            action = (record.get("action") or "").lower()
            path = (record.get("path") or "").lower()
            if needle != action and needle not in path:
                continue
            detail = {
                "at": record.get("at"),
                "method": record.get("method"),
                "url": record.get("url"),
                "action": record.get("action"),
                "status": record.get("status"),
                "headers": record.get("headers", {}),
                "header_payload": record.get("header_payload", False),
                "ui_hint": record.get("ui_hint", ""),
                "body": record.get("body"),
            }
            if include_response_shape:
                detail["response_shape"] = record.get("response_shape")
            matches.append(detail)
            if len(matches) >= max(1, limit):
                break

        if not matches:
            return json.dumps({
                "error": f"No captured call matches '{endpoint}' in session '{resolved_id}'.",
                "hint": "Endpoint names come from the classification report's `endpoint` column.",
            })

        return json.dumps({
            "success": True,
            "session_id": resolved_id,
            "endpoint": endpoint,
            "count": len(matches),
            "calls": matches,
            "transport_hint": (
                "header_payload=true means the JSON travelled URL-encoded in X-OWA-UrlPostData, "
                "so implement it with client.request_header_payload(); a path that isn't "
                "/owa/service.svc needs client.request_substrate() and only exists on the "
                "modern bearer backend."
            ),
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"Failed to read discovery detail: {e}"})
