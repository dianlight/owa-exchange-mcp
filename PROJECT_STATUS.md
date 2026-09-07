# Exchange MCP Server — Project Status

_Snapshot generated 2026-09-04 from the working tree (`main`, uncommitted changes on top of `413c31a`)._

## 1. Where the project stands

The server is mid-migration: the transport layer moved from a plain `requests`-based
HTTP client (cookie file, hand-rolled headers) to a **persistent Chromium browser
session** ([browser_session.py](exchange_mcp/browser_session.py)), because OWA now
requires signals (fresh per-page CSRF canary, real browser headers, a genuine TLS/JS
fingerprint) that a replayed-cookie HTTP session can no longer fake.

This migration touches every file in the request path, and as of this snapshot it is
**uncommitted** (`git status` shows all of the files below as modified, plus two new
untracked files):

| File | State |
|---|---|
| `exchange_mcp/browser_session.py` | **New** — owns the persistent Chromium context, canary/bearer auth, retry-on-crash |
| `exchange_mcp/owa_client.py` | Rewritten to delegate 100% of transport to `BrowserSession` |
| `exchange_mcp/server.py` | Lifespan now launches/stops the browser instead of a plain HTTP session |
| `exchange_mcp/auth.py` | Login glue rewritten against `BrowserSession` |
| `login.py` | Rewritten for browser-based 2FA login |
| `exchange_mcp/tools/*.py` (all 7 modules) | Adjusted to the new client surface (no direct HTTP calls in any tool — see §3) |
| `_diag_session_valid.py` | **New**, untracked — ad-hoc manual script to check `ensure_logged_in()` / session validity, not part of the package |

**Net effect for the tool table below:** because every tool module talks to Exchange
exclusively through `OWAClient.request()` / `OWAClient.request_header_payload()`, and
those two methods are now fully backed by `BrowserSession`, **all 30 tools are
architecturally migrated** — there is no tool left calling the old HTTP path directly.
What has *not* happened yet is validation: there is no `tests/` directory in this repo,
and I have no evidence in this session that the new transport has been exercised
end-to-end against a live OWA server for each tool (only the login/session-validity path
has a dedicated manual script).

`
✶ Insight ─────────────────────────────────────
`OWAClient` is a thin facade: `request()` and `request_header_payload()` are the *only*
two methods every tool module imports and calls. That single choke point is why the
migration could be done in `browser_session.py`/`owa_client.py` alone — no tool file
needed to change its business logic, only (in a few cases) minor payload shape tweaks
for the "modern Outlook" bearer-auth path (`get_folder_id()` now branches on
`browser.auth_mode`). This is a textbook case of an abstraction boundary paying for
itself during a transport swap.
─────────────────────────────────────────────────
`

**Also uncommitted, same snapshot — transport option, not a tool-level change:**
[server.py](exchange_mcp/server.py) gained a `--transport {stdio,http}` flag for running
it as a persistent long-lived process instead of a per-session stdio spawn. There is no
autostart mechanism — it must be started manually. This doesn't change any tool's
Migration/Automated-test/Manual-QA status below — every tool still goes through the same
`OWAClient`/`BrowserSession` regardless of which transport carries the MCP session.

## 2. How to read the table

- **Migration** — `Migrated` means the tool's code path goes exclusively through the
  browser-backed `OWAClient`. Nothing in this codebase is `Unmigrated` today.
- **Automated test** — this repo has **no test suite** (no `tests/` folder, no CI
  config found). So this column is `None` for every row — it is a statement about the
  repo, not about any individual tool.
- **Manual QA / Status** — whether the tool has actually been exercised against a real
  OWA mailbox since the browser-session rewrite, and the observed result. I have not run
  any of these tools myself in this session (that would require a live `EXCHANGE_OWA_URL`,
  stored credentials, and — for first login — your 2FA approval), so every row defaults to
  **`Pending`** unless you tell me otherwise. Update this column as you validate each tool;
  use `OK` / `KO` once you have an actual result, and add a one-line note (error text,
  date) for any `KO`.

## 3. Tool inventory (30 tools across 7 modules)

### Email — [exchange_mcp/tools/email.py](exchange_mcp/tools/email.py) (10)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `get_emails` | List emails from a folder, grouped by conversation/thread, with unread/pagination filters | Migrated | None | Pending |
| `get_email` | Get a single email's full body, recipients, and attachments | Migrated | None | Pending |
| `send_email` | Send a new email (to/cc/bcc, HTML or plain text) | Migrated | None | Pending |
| `reply_email` | Reply (or reply-all) to an email | Migrated | None | Pending |
| `forward_email` | Forward an email to new recipients | Migrated | None | Pending |
| `mark_email_read` | Mark one or more emails read/unread | Migrated | None | Pending |
| `move_email` | Move one or more emails to another folder | Migrated | None | Pending |
| `delete_email` | Delete (soft or permanent) one or more emails | Migrated | None | Pending |
| `download_attachments` | Download all file attachments from an email to disk | Migrated | None | Pending |
| `get_email_links` | Extract hyperlinks from an email's HTML body | Migrated | None | Pending |

### Calendar — [exchange_mcp/tools/calendar.py](exchange_mcp/tools/calendar.py) (7)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `get_calendar_events` | List events in a date range; optional recurring-occurrence expansion | Migrated | None | Pending |
| `create_meeting` | Create a meeting with attendees, location, reminder, sensitivity | Migrated | None | Pending |
| `update_meeting` | Update a meeting (implemented as cancel + recreate — OWA JSON API has no reliable `UpdateItem` for calendar items) | Migrated | None | Pending |
| `cancel_meeting` | Cancel a meeting and notify attendees | Migrated | None | Pending |
| `respond_to_meeting` | Accept / decline / tentatively accept a meeting invite | Migrated | None | Pending |
| `download_event_attachments` | Download file attachments from a calendar event | Migrated | None | Pending |
| `get_event_links` | Extract hyperlinks from an event's HTML description | Migrated | None | Pending |

### Directory — [exchange_mcp/tools/people.py](exchange_mcp/tools/people.py) (1)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `find_person` | Search Active Directory (`ResolveNames`) for people by name/email/keyword | Migrated | None | Pending |

### Folders — [exchange_mcp/tools/folders.py](exchange_mcp/tools/folders.py) (7)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `check_session` | Lightweight auth check (`FindFolder` on inbox) — reports mailbox name + unread count | Migrated | None | Pending — related manual script exists (`_diag_session_valid.py`), but it checks `BrowserSession.ensure_logged_in()` directly, not this MCP tool |
| `get_folders` | List mail folders (shallow or recursive) with counts | Migrated | None | Pending |
| `create_folder` | Create a new mail folder | Migrated | None | Pending |
| `rename_folder` | Rename an existing folder | Migrated | None | Pending |
| `empty_folder` | Empty all items from a folder | Migrated | None | Pending |
| `delete_folder` | Delete a mail folder | Migrated | None | Pending |
| `move_folder` | Move a folder under a different parent | Migrated | None | Pending |

### Availability — [exchange_mcp/tools/availability.py](exchange_mcp/tools/availability.py) (2)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `find_free_time` | Find free slots in your own calendar within working hours | Migrated | None | Pending |
| `find_meeting_time` | Find common free slots across multiple attendees (`GetUserAvailability`) | Migrated | None | Pending |

### Analytics — [exchange_mcp/tools/analytics.py](exchange_mcp/tools/analytics.py) (2)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `get_meeting_stats` | Meeting-count statistics for one or more people over a date range | Migrated | None | Pending |
| `get_meeting_contacts` | Weighted "who you meet with most" connection matrix from your own calendar | Migrated | None | Pending |

### Auth — [exchange_mcp/tools/auth.py](exchange_mcp/tools/auth.py) (1)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `login` | Credential setup + two-call, non-blocking 2FA login against the shared browser session | Migrated | None | Pending — closest to being exercised: `_diag_session_valid.py` drives the same `BrowserSession.ensure_logged_in()` this tool calls, but as a standalone script outside the MCP server process |

## 4. Gaps worth closing

- **No automated tests.** Nothing in this repo currently asserts, e.g., that
  `_build_recipient_list` handles empty/whitespace addresses correctly, or that
  `folder_id_dict()` picks the right `__type` for a distinguished vs. opaque folder ID —
  both are pure-ish functions that would be cheap to unit test without a live mailbox.
- **No live/manual QA log.** There's no record (changelog, issue tracker, etc.) of which
  of the 30 tools have actually been run against a real OWA mailbox since the
  browser-session rewrite. This document's "Manual QA / Status" column is a template for
  that log — fill it in as you verify each tool.
- **Stale tool count in [CLAUDE.md](CLAUDE.md).** It still says "20 tools"; the actual
  count (per `server.py`'s tool registrations and the README) is 30. Worth a one-line fix
  next time that file is touched.
