---
name: owa-capability-discovery
description: Interactively discover what OWA/Exchange can do that this MCP server doesn't implement yet. Records a live, user-driven OWA session in a fresh isolated browser, then classifies every captured API call as an unknown API, a known API with parameters we never send, or a whole new capability class relative to the declared scope — and proposes concrete new/modified MCP tools with permanent IDs. Use when the user wants to explore, discover, reverse-engineer, or map OWA capabilities, find API gaps, plan new tools, or asks "what else can OWA do", "record what I do in OWA", "is there an API for <OWA feature>", "capture the calls for <feature>".
---

# OWA capability discovery

Drive a recorded exploration of a live OWA mailbox and turn it into an
implementation plan for this server.

The mechanics live in six MCP tools (module 11, `exchange_mcp/tools/discovery.py`);
your job is the interactive part: pin down a useful scope, get the user through
the recording, and turn the classified result into decisions.

## Preconditions

The Exchange MCP server must be connected and expose `start_discovery_session`.
If it doesn't, the server predates this feature or is running an older install —
say so rather than improvising a workaround.

The recording opens a **second, independent browser** on a **brand-new empty
profile**. Two consequences to state up front, because both surprise people:

- **The user must sign in inside the recording window.** No existing session is
  reused. That is the point: a clean profile can't inherit cached SPA state, and
  nothing done in the recording can damage the profile the server serves from.
- Sign-in traffic is dropped from the capture, and no token, cookie or canary is
  ever written to disk. You can say this plainly if the user asks.

## 1. Establish the scope

The scope decides which observations count as relevant and what a newly
discovered capability class gets named, so a vague scope produces a vague
report. If the user hasn't given one, ask with `AskUserQuestion` — offer
concrete OWA areas, e.g.:

- Inbox rules / automatic replies (out-of-office)
- Mailbox settings, signatures, language and time zone
- Delegates, calendar sharing and folder permissions
- Contacts, groups or notes management

Push for one coherent area rather than "everything": a capture spanning six
unrelated features produces findings nobody acts on. A good scope is a sentence
the user could hand to a colleague — *"managing inbox rules: creating one,
reordering, disabling"*.

Also worth asking once, only if relevant: if they want to inspect **response
data** (not just field names), pass `capture_response_bodies=True`. Default off
keeps mailbox content out of the capture files.

## 2. Start the recording

```
start_discovery_session(scope="<their words>", notes="<optional>", start_url=<optional>)
```

Pass `start_url` when the scope is a specific settings page — it saves the user
several clicks and keeps the capture focused.

Relay the returned `user_instructions` to the user as a short numbered list.
Emphasise the last one: **closing the browser window is what ends the
recording.**

## 3. Wait

Poll `get_discovery_status` — not in a tight loop. A sign-in plus a real
exploration is minutes, so wait between polls (a `sleep 45` in Bash is fine) and
tell the user you're waiting. Stop polling and just ask if it drags on well past
what the scope should take; they may have got distracted, in which case
`stop_discovery_session` ends it cleanly.

Watch the counters. If `api_calls` stays at 0 while `state` is `recording`, the
sign-in almost certainly hasn't been completed — say so instead of waiting
silently.

## 4. Classify

Once `state` is `finished`:

```
classify_discovery_session()
```

This writes `report.json` and `report.md` into the capture directory and returns
the summary. Read the response; you do not need to re-read the files.

## 5. Present the summary

Structure it for a decision, not as a data dump:

1. **One line of context** — what was captured, how many distinct endpoints, how
   many in scope.
2. **The three proposal groups**, in-scope first, skipping any that are empty:
   - **New tool modules** (`proposals.new_modules`) — a capability class this
     server has no module for. Give the proposed module number, file, and each
     tool's proposed ID.
   - **New tools in existing modules** (`proposals.new_tools`).
   - **Changes to existing tools** (`proposals.extend_tools`) — name the tool and
     the parameters OWA sends that we never do, and what they'd enable.
3. For each proposal, one plain sentence on **what the user would gain**. "OWA
   has a `GetInboxRules` action we never call — a `get_inbox_rules` tool would let
   an agent read the mailbox's rules" beats restating the table.
4. **What was already covered**, in one line. It's evidence the baseline works.

Offer the rendered `report.md` with `SendUserFile` if the report is long.

Then ask what to do next: implement one or more now, record them as gaps in
PROJECT_STATUS.md §4, or stop here.

## 6. Implement (only if asked)

Before writing any code, get the real wire traffic:

```
get_discovery_detail(endpoint="<endpoint from the report>")
```

Then follow `references/implementation-checklist.md` in this skill directory —
it covers the module conventions, the transport choice, and every file that has
to be updated alongside the code.

## Honesty rules

These matter more than usual here, because the output is a plan someone will act
on:

- **Never propose an endpoint that isn't in the capture.** If the user expected a
  feature to show up and it didn't, the honest answer is "OWA didn't call
  anything new for that — it may be client-side, or it may need a different
  interaction", not a plausible-sounding action name.
- **Verdicts are heuristics and say so.** They come from a static scan of this
  repo plus keyword tables in `exchange_mcp/capability_classify.py`, biased
  toward under-reporting. If a finding's evidence contradicts its label, say so
  and fix the table — that's a one-line edit, and it's where the domain
  knowledge is meant to live.
- **`known_api_covered` is a real result.** A session that discovers nothing new
  means the scope is already implemented. Report that as the finding it is
  instead of manufacturing work from the covered rows.
- **Never commit a capture directory.** `.discovery-sessions/` is gitignored: it
  holds real mailbox metadata (field names, folder names, UI labels).
