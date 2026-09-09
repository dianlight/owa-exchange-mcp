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
those two methods are now fully backed by `BrowserSession`, **all tools are
architecturally migrated** — there is no tool left calling the old HTTP path directly.

**Update 2026-09-07 — smoke-test suite in progress.** A `tests/smoke/` package now
exists: `server_manager.py` starts/reuses a persistent `--transport http --show-browser`
instance (never auto-stopped), `mcp_client.py`/`results.py` are shared helpers, and one
test module per tool is being added incrementally under `tests/smoke/tests/`, run and
approved individually before moving to the next tool. Two real bugs surfaced and were fixed so far:
1. `check_session` (see its row below) — fixed in [folders.py](exchange_mcp/tools/folders.py).
2. A transport-level race in [browser_session.py](exchange_mcp/browser_session.py)'s
   `_async_execute_on_anchor` (bearer/modern-Outlook path only): the SPA can navigate
   itself internally at any point, and if that happens mid-`page.evaluate()` it destroys
   the JS execution context, failing the in-flight request with "Execution context was
   destroyed" — hit consistently on `get_email`/`get_email_links` (any GetItem-shaped
   call after the first request in a session). Fixed with a one-retry-after-settle in
   that method; no full browser relaunch or re-login needed. This affects every tool that
   calls `client.request()`/`client.request_header_payload()` on this backend, so watch
   for it while testing the rest of the tools below.

All 10 tools in `email.py`, all 7 tools in `calendar.py`, plus `check_session`, are now
`OK`. The mutating email tools (`send_email`/`reply_email`/`forward_email`/
`mark_email_read`/`move_email`/`delete_email`/`download_attachments`) share one chained,
self-cleaning test ([test_email_lifecycle.py](tests/smoke/tests/test_email_lifecycle.py))
built around a single disposable, uniquely-tagged self-addressed message. Sending/
replying/forwarding to your own mailbox creates several distinct physical items (Inbox
delivery, Sent Items copy) under the same subject tag, and the *delivery* copies (as
opposed to the synchronous Sent Items copy `SendAndSaveCopy` writes at send time) can
land well after the tool call returns — a single fixed-delay sweep-and-delete missed
stragglers twice in a row during development. The cleanup step now loops sweep-then-delete
until a full pass finds nothing left (bounded at 6 attempts, 10s apart) rather than
sweeping once.

`create_meeting`/`update_meeting`/`cancel_meeting`/`download_event_attachments`/
`get_event_links` share a similar chained test
([test_calendar_lifecycle.py](tests/smoke/tests/test_calendar_lifecycle.py)) around one
disposable, uniquely-tagged self-invited meeting. `respond_to_meeting` could not be
covered by that same pattern: self-inviting (organizer == sole attendee) never produces a
meeting-request email to respond to, confirmed by direct observation — Exchange doesn't
ask an organizer to accept an invite they already own directly on their calendar. It was
instead verified manually against a real incoming Google Calendar invite from a different
account (see its row below); a fully automated, self-contained test for it isn't possible
without a second mailbox.

The transport-level "execution context was destroyed" race (bullet 2 above) recurred once
more during calendar testing on a fresh `get_email` call, confirming the fix's one retry is
still necessary and sufficient — it succeeded on the retry both times observed so far.

`get_folders`, plus the remaining 5 mutating tools in `folders.py`
(`create_folder`/`rename_folder`/`move_folder`/`empty_folder`/`delete_folder`), are now `OK`
too. The 5 mutating tools share one chained test
([test_folder_lifecycle.py](tests/smoke/tests/test_folder_lifecycle.py)) around one
disposable, uniquely-tagged folder: create under Inbox → rename → move to `msgfolderroot` →
drop one disposable tagged email into it (via the already-verified `send_email`/`move_email`)
→ empty (verified gone) → permanently delete. The move-before-populate ordering wasn't
arbitrary at the time: `move_email`'s `target_folder` and `get_emails`'s `folder` argument both
resolved a folder *name* via a **Shallow** `FindFolder` search rooted at `msgfolderroot`
([owa_client.py](exchange_mcp/owa_client.py) `get_folder_id()`), so a folder nested one level
under Inbox was invisible to those lookups until `move_folder` promoted it to a direct child of
`msgfolderroot`. **Superseded 2026-09-09** — see the update below; `get_folder_id()` now walks
`/`-delimited paths, so this ordering constraint no longer applies (kept here for historical
context on why the original test was written this way).

A one-off operational incident during this test (not a tool bug, logged for awareness):
after a long idle gap mid-session, the persistent browser session's `launch_persistent_context`
started failing with "Target page, context or browser has been closed" on every call — an
orphaned Chromium process tree from the original launch was still holding `.browser-profile`'s
singleton lock, so even `BrowserSession`'s built-in one-retry-after-crash recovery couldn't get
a fresh context. Required a manual `taskkill /PID <pid> /T /F` on the tracked server process
(which cleaned up the whole orphaned tree) followed by a fresh server start; see the gap note
below.

**Update 2026-09-08 — people/availability/analytics tools tested; one architecture bug and
two error-handling bugs found and fixed.** `find_person`, `find_free_time`, `find_meeting_time`,
`get_meeting_stats`, and `get_meeting_contacts` are now covered (see their rows below); only
`login` remains untested. Two genuine, unfixable-in-this-codebase Microsoft server-side
limitations on this tenant surfaced and account for every `KO` below: `ResolveNames` throws a
server-side `NullReferenceException` (breaks `find_person`, `get_meeting_stats`), and
`GetUserAvailability` isn't implemented at all — it returns
`{ErrorCode: 500, ExceptionName: NotImplementedException}` (breaks `find_meeting_time`, and
silently degraded `get_meeting_stats`/`get_meeting_contacts` until fixed, below).

Also found and fixed a significant architecture bug in [server.py](exchange_mcp/server.py),
unrelated to any single tool: the mcp SDK's `StreamableHTTPSessionManager` runs a **fresh
lifespan per client session** under `--transport http`, not once per process — contradicting
this document's and CLAUDE.md's prior claim that the browser/login is "shared across every
client connection." In practice, every single MCP connection made during this whole smoke-test
effort had been launching its own Chromium instance and logging in again, then tearing it down
when that connection closed. First surfaced as `get_meeting_contacts` intermittently failing
with "User email not available" (a startup race against the now-understood fresh-lifespan-per-session
launch). Fixed with a module-level singleton (`_get_shared_client()`, guarded by an `asyncio.Lock`)
created on first use and torn down only at process exit (`atexit`), not per-session; verified live
via server.log — a second, independent client session no longer triggers a new "Launching browser"
entry. This also reframes the browser-profile-lock gap note in §4 below: lock contention from
overlapping browser launches was a structural risk on every multi-session run, not just the
one-off idle-gap crash originally described there.

While diagnosing `find_meeting_time` and `get_meeting_contacts`/`get_meeting_stats`, fixed two
more real bugs, both in error handling rather than the OWA calls themselves — see their tool rows
below for details: a `FaultMessage`-fallback bug in [availability.py](exchange_mcp/tools/availability.py)
(`.get()`'s default doesn't apply when the key is present but explicitly `null`), and a silent
`except Exception: pass` in [analytics.py](exchange_mcp/tools/analytics.py)'s
`_get_availability_events()` that masked the `GetUserAvailability` failure as a false-clean "0
meetings" result in both `get_meeting_stats` and `get_meeting_contacts`.

**Update 2026-09-08 (continued) — `login` tested; one more architecture bug found and fixed.**
Verified via the full forced-relogin flow: moved `.browser-profile` aside (kept as a
timestamped backup, not deleted), restarted the server without `EXCHANGE_MASTER_PASSWORD`
so it came up with no session, confirmed via `check_session` that the session was genuinely
invalid, called `login(master_password=...)` to trigger a real decrypt + browser 2FA login,
approved the mobile push, then called `login()` again to harvest the result — `check_session`
came back authenticated afterward, confirming the real 2FA path works end-to-end.

While doing this, found that the *second* `login()` call reported `"Session is already
active"` (the "no pending task" branch) instead of `"Logged in and session verified."` (the
"harvested a background 2FA result" branch) — surfacing a real bug: [server.py](exchange_mcp/server.py)'s
`AppContext.pending_login` was a per-instance dataclass field, but `AppContext` itself is
created fresh on every `app_lifespan()` call (i.e. per client session), while the
`OWAClient`/browser it wraps is the shared process-wide singleton (see the lifespan fix
above). So the login tool's two-call 2FA pattern only worked if both calls happened to land
on the *same* MCP client session — a second call from a different session (as happened here,
since the verification script opened a fresh connection each run) never saw the first call's
background task, and would have silently started a duplicate `perform_login()` if the first
one hadn't already finished by the time it ran. It "worked" in this verification only because
the real login had already completed by the time the second call landed; a slower 2FA
approval would have raced a second login attempt against the first on the same browser.
Fixed by moving `pending_login` out of the per-instance field and into the same
module-level shared-state group as `_shared_client`/`_shared_browser`, exposed through a
property on `AppContext` so `auth.py` needed no changes. [test_login.py](tests/smoke/tests/test_login.py)
covers the repeatable "already active" idempotent path (regression-verified against the fix
after restarting the server); the actual cross-session background-task-harvest branch was
verified by code inspection (single `AppContext(...)` construction site, `py_compile` clean)
rather than a second live 2FA cycle, since forcing another mobile-push approval just to
re-exercise a small, mechanically-obvious fix wasn't worth asking for.

**Update 2026-09-08 (continued) — new Category tools added (email + calendar + master
list).** Added a new `categories.py` module (4 tools: `list_categories`/`create_category`/
`rename_category`/`delete_category`) plus 3 category tools each on `email.py` and
`calendar.py` (`assign_*_categories`/`remove_*_categories`/`find_*_by_category`), using
OWA's native "Category" terminology. `categories.py`'s master-list CRUD goes through a
bespoke, non-EWS `UpdateMasterCategoryList` action (see its own module docstring — the
classic EWS `GetUserConfiguration`/`UpdateUserConfiguration` pattern 500s with a
`NullReferenceException` on this tenant for the `"CategoryList"` config name). The
email-side per-item tools use the standard EWS `UpdateItem`/`SetItemField` action and work
correctly.

At the time, the calendar-side per-item write tools did not: `assign_event_categories`/
`remove_event_categories` could not persist a `Categories` change on this OWA build via the
standard EWS `UpdateItem`/`SetItemField` action — see the follow-up update below for the fix.
Getting here surfaced and fixed a real false-positive-success bug along the way: OWA's
`UpdateItem` action can return a top-level fault envelope (`{"Body": {"ErrorCode": ...,
"FaultMessage": ...}}`) with no `ResponseMessages` key at all, which
`OWAClient.extract_items()` silently treats as "no items, so no error" — every existing
`UpdateItem`-based tool that only checked per-item `ResponseClass == "Error"` was exposed to
this exact class of silent failure. Fixed centrally in
[owa_client.py](exchange_mcp/owa_client.py)'s `_to_json()`, which now raises immediately on
that fault-envelope shape, so any current or future caller gets a real error instead of a
false "success" — this is a general hardening, not specific to categories.
`find_events_by_category` (a pure `FindItem`+`CalendarView` read, unaffected by the write
bug) works correctly.

All 40 tools have now been exercised at least once against a real OWA mailbox.

**Update 2026-09-08 (continued) — fixed the category write-path on calendar items.**
`assign_event_categories`/`remove_event_categories` were failing because standard EWS
`UpdateItem`/`SetItemField` unconditionally rejects `CalendarItem` updates on this OWA build
with `ErrorSendMeetingInvitationsOrCancellationsRequired`, no matter what
`SendMeetingInvitationsOrCancellations`/`...Specified` combination is sent. Resolved by
driving the real OWA web UI with Playwright against the persistent Chromium profile and
capturing the actual request its own client sends when toggling a category on an
already-saved event (headers redacted before anything was printed or written to disk, per
the project's standing rule against persisting live bearer tokens). OWA's web client doesn't
use `UpdateItem` for this at all — it POSTs a bespoke `UpdateCalendarEvent` action via the
`X-OWA-UrlPostData` header (empty POST body, same transport `categories.py`'s
`UpdateMasterCategoryList` already uses), with a singular `ItemChange`, a top-level `EventId`
mirroring `ItemChange.ItemId`, no `ChangeKey`/`ConflictResolution`, and
`ShouldSendUpdateToAttendees`/`EventScope`/`TargetAudience` in place of
`SendMeetingInvitationsOrCancellations`. `_set_event_categories` in
[calendar.py](exchange_mcp/tools/calendar.py) now builds and sends that exact shape via
`OWAClient.request_header_payload()`; re-run against a real mailbox via
`test_calendar_category_tagging.py`, which now passes end to end.

**Update 2026-09-08 (continued) — `get_calendar_events` returned empty results.**
Reported as a live bug: real calendar items confirmed to exist via direct `GetItem`
lookups were not showing up in `get_calendar_events` for date ranges that definitely
contained them, reproduced on both a freshly created zero-attendee appointment and
wide multi-month ranges. Root-caused to two independent, unrelated bugs, not a
session/canary degradation:

1. `get_calendar_events`'s default and `expand_recurring=True` modes both treated
   `GetUserAvailability` as the authoritative event source (`FindItem` results were
   only used to enrich matches already found there). `GetUserAvailability` returns a
   permanent `{"ErrorCode": 500, "ExceptionName": "NotImplementedException"}` fault on
   this OWA build regardless of `RequestedView` — confirmed across all five documented
   values. Since that call always failed (silently, into a swallowing `except
   Exception: pass`), the "authoritative" list was always empty, so the merge step
   never had anything to enrich. Fixed by dropping `GetUserAvailability` entirely and
   making `FindItem`+`CalendarView` the sole source, mirroring the pattern
   `find_events_by_category` already used successfully. The `expand_recurring`
   parameter was removed rather than fixed: it depended entirely on
   `GetUserAvailability`'s expansion, and this backend's `CalendarView` does not expand
   recurring series into per-occurrence items either (a series master item's own
   `CalendarItemType` is `RecurringMaster`, appearing once, not once per occurrence —
   verified directly by fetching a real 3-month range and finding zero
   `Occurrence`/`Exception`-typed items alongside 147 `RecurringMaster` ones). This is a
   narrower documented limitation, not a regression: no code path on this deployment
   can currently expand recurring series into occurrences.
2. Independently, and more severely: `FindItem`'s `CalendarView.StartDate`/`EndDate`
   has **no filtering effect at all** on this OWA build — verified by querying the
   same folder with a 1-day window, a window in the year 1901, and a window in the
   year 2099, all three returning the identical ~4800-item count (the entire calendar
   folder, unfiltered). This affected both `get_calendar_events` and
   `find_events_by_category` (the latter was previously marked `OK` because its
   category-match assertions happened to still pass — the date-range parameters were
   silently no-ops the whole time). Fixed by filtering `FindItem`'s results
   client-side against each item's own `Start`/`End` in both tools
   (`_filter_items_by_date_range` in [calendar.py](exchange_mcp/tools/calendar.py)).

Also strengthened [test_get_calendar_events.py](tests/smoke/tests/test_get_calendar_events.py):
the old version only asserted the tool returned *a list*, which an always-empty result
satisfied just as well as a correct one — exactly how this bug went undetected. It now
creates a disposable appointment inside the query window and asserts it comes back by
`item_id`, then cleans up.

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
Automated-test/Manual-QA status below — every tool still goes through the same
`OWAClient`/`BrowserSession` regardless of which transport carries the MCP session.

**Update 2026-09-09 — new tool `search_emails` added, then reworked after two more
tenant-side EWS limitations surfaced.** The first implementation used `FindItem`'s
`QueryString` (AQS syntax) directly and returned zero results for every query,
including one built from a word verified present in a real subject line — a genuine,
unfixable content-index failure on this tenant, not a syntax bug (confirmed
`QueryString` must also be the typed object `{"__type": "QueryStringType:#Exchange",
"Value": ...}`, not a plain string, or `FindItem` throws outright). Separately,
`FindItem`'s `Traversal:"Deep"` (needed for `search_all_folders=True`) throws
`"Invalid argument used to call method FindItem"` unconditionally on this tenant,
regardless of folder or `QueryString` — an entirely different limitation from the
content-index one. Rather than mark the tool `Dev`/`KO`, implemented an AQS-then-
fallback design: try the server-side `QueryString` search first (`Shallow`, per
folder); if it's empty or throws, fall back to a client-side scan of the same
folder(s) (`_local_search_fallback` in [email.py](exchange_mcp/tools/email.py)),
matching a reduced AQS-lite subset (bare terms, `subject:`, `from:`, `category:`,
`isread:`, `hasattachment:`) against fields already present in the `Default` shape
response. For `search_all_folders=True`, folders are enumerated via `FindFolder`/
`Deep` (`_list_all_folder_ids`) — a different EWS operation that works fine on this
tenant, already used by `get_folders(recursive=True)` — then searched one at a time
with `Shallow` `FindItem`, aggregating results until `limit` is reached. Both paths
verified live: single-folder (AQS-empty-then-fallback) via
[test_search_emails.py](tests/smoke/tests/test_search_emails.py), and
`search_all_folders=True` via a direct MCP call returning results with no error.

**Update 2026-09-09 — `move_email` couldn't resolve nested destination folders.** Reported
from real usage: every attempt to move an email into any of several `Progetti/*` subfolders
(e.g. `Progetti/ACE-NewGeco`) failed with "Folder not found," trying both the full path and
the bare folder name (10 attempts, all failed) — consistent with an earlier failure moving
into `Quarantena`, a folder nested one level under Inbox. Root cause: `get_folder_id()`
([owa_client.py](exchange_mcp/owa_client.py)) only ran a single **Shallow** `FindFolder`
rooted at `msgfolderroot` with a literal `DisplayName` match, so it could see direct children
of `msgfolderroot` and nothing else — a folder nested under another custom folder, or under a
distinguished folder like Inbox, was invisible to it regardless of whether the caller passed
the bare name or a `/`-delimited path (no path-splitting logic existed at all; a literal
string like `"Progetti/ACE-NewGeco"` could never equal a single-segment `DisplayName`).
Confirmed live against the real mailbox structure that `Progetti` is top-level with
`ACE-NewGeco` nested inside it, that a distinct top-level folder `ACE - NewGeco` (with spaces)
also exists — ruling out a naive Deep-search-by-bare-name fallback as unsafe/ambiguous — and
that `Quarantena` is nested one level under `Posta in arrivo` (Inbox).

Fixed by adding `_resolve_folder_path()`/`_find_child_folder_id()` to `get_folder_id()`: a
`/`-delimited path is now walked one Shallow `FindFolder` per segment, starting from a
distinguished folder (e.g. `Inbox`) when the first segment names one, otherwise from
`msgfolderroot`, resolving each subsequent segment as a child of the previous. This is
additive — bare single-segment names (the only other call shape used anywhere in the
codebase, by `email.py`/`calendar.py`/`analytics.py`/`availability.py`) are unaffected. New
regression test [test_move_email_nested_folder.py](tests/smoke/tests/test_move_email_nested_folder.py)
reproduces both bug shapes from the report against disposable, uniquely-tagged folders (a
custom folder nested under another custom folder, and a folder nested under the Inbox
distinguished folder) rather than the real `Progetti`/`Quarantena` trees; both cases verified
live end-to-end (move + confirm landed at the nested path), passing after the fix.

## 2. How to read the table

- **ID** — a permanent 3-digit identifier: digit 1 is the module number (fixed per
  module, see the module list below), digits 2-3 are the tool's sequence number within
  that module. Once assigned, a tool's ID never changes or gets reused, even if the
  table is reordered or tools are added/removed elsewhere — see CLAUDE.md's "Maintaining
  PROJECT_STATUS.md" section for the assignment rule. Module numbers: 1 Email, 2
  Calendar, 3 Categories, 4 Directory, 5 Folders, 6 Availability, 7 Analytics, 8 Auth.
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
- **Stability** — `Stable` unless the tool has a confirmed, unfixable server-side fault
  (currently `find_person` #401, `find_meeting_time` #602, `get_meeting_stats` #701 — all
  three `KO` above), in which case it's `Dev`. This is a narrower bar than `KO`/`Pending`:
  a tool that degrades gracefully instead of failing (e.g. `get_meeting_contacts` #702,
  which returns an empty result plus a `warnings` field rather than erroring) stays
  `Stable`, and an untested (`Pending`) tool also stays `Stable` by default — only move a
  tool to `Dev` once it's a confirmed, currently-unfixable failure. `Dev`-tagged tools are
  exactly the set in `KNOWN_BUGGY_TOOLS` in [server.py](exchange_mcp/server.py), which the
  `--stable` CLI flag / `EXCHANGE_MCP_STABLE` env var excludes from the MCP tool listing at
  startup — keep the two in sync (see CLAUDE.md's "Maintaining PROJECT_STATUS.md" section).

## 3. Tool inventory (41 tools across 8 modules)

### Email — [exchange_mcp/tools/email.py](exchange_mcp/tools/email.py) (14)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 101 | `get_emails` | List emails from a folder, grouped by conversation/thread, with unread/pagination filters | `tests/smoke/tests/test_get_emails.py` | OK (2026-09-07) | Stable |
| 102 | `get_email` | Get a single email's full body, recipients, and attachments | `tests/smoke/tests/test_get_email_detail.py` | OK (2026-09-07) | Stable |
| 103 | `send_email` | Send a new email (to/cc/bcc, HTML or plain text) | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 104 | `reply_email` | Reply (or reply-all) to an email | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 105 | `forward_email` | Forward an email to new recipients | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 106 | `mark_email_read` | Mark one or more emails read/unread | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 107 | `move_email` | Move one or more emails to another folder; `target_folder` accepts a bare name (direct child of `msgfolderroot`/a distinguished folder) or a `/`-delimited path for folders nested deeper (e.g. `Progetti/ACE-NewGeco`, `Inbox/Quarantena`) | `tests/smoke/tests/test_email_lifecycle.py`, `tests/smoke/tests/test_move_email_nested_folder.py` | OK (2026-09-09, re-verified) — see "Update 2026-09-09 — move_email couldn't resolve nested destination folders" below | Stable |
| 108 | `delete_email` | Delete (soft or permanent) one or more emails | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 109 | `download_attachments` | Download all file attachments from an email to disk | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 110 | `get_email_links` | Extract hyperlinks from an email's HTML body | `tests/smoke/tests/test_get_email_detail.py` | OK (2026-09-07) | Stable |
| 111 | `assign_email_categories` | Add one or more categories to emails, keeping any already present | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-08) | Stable |
| 112 | `remove_email_categories` | Remove one or more categories from emails, keeping any others present | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-08) | Stable |
| 113 | `find_emails_by_category` | Find email conversations tagged with a given category | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-08) | Stable |
| 114 | `search_emails` | Full-text search for emails, scoped to one folder or the whole mailbox. Tries EWS `FindItem`/`QueryString` (AQS syntax: `subject:`, `from:`, `body:`, `received:`, etc.) first, then transparently falls back to a client-side scan (reduced keyword subset: bare terms, `subject:`, `from:`, `category:`, `isread:`, `hasattachment:`) — this tenant's content index never returns AQS results, and `FindItem`'s `Traversal:"Deep"` is unsupported outright, so `search_all_folders` enumerates folders via `FindFolder`/`Deep` (like `get_folders`) and searches each one `Shallow` | `tests/smoke/tests/test_search_emails.py` | OK (2026-09-09) — single-folder AQS-empty + fallback, and `search_all_folders=True` across folders, both verified live | Stable |

### Calendar — [exchange_mcp/tools/calendar.py](exchange_mcp/tools/calendar.py) (10)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 201 | `get_calendar_events` | List events in a date range (a recurring series appears once, as its master item, not expanded per occurrence) | `tests/smoke/tests/test_get_calendar_events.py` | OK (2026-09-08, re-verified) — see "Update 2026-09-08 (continued) — get_calendar_events returned empty results" below | Stable |
| 202 | `create_meeting` | Create a meeting with attendees, location, reminder, sensitivity | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 203 | `update_meeting` | Update a meeting (implemented as cancel + recreate — OWA JSON API has no reliable `UpdateItem` for calendar items) | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 204 | `cancel_meeting` | Cancel a meeting and notify attendees (soft-delete only — moves to Deleted Items, no permanent-delete option) | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 205 | `respond_to_meeting` | Accept / decline / tentatively accept a meeting invite | None — self-invite produces no meeting-request email to respond to (confirmed 2026-09-08; Exchange doesn't ask an organizer to accept their own invite), so this can't be covered by a self-contained automated test | OK (2026-09-08, manual) — verified against a real incoming Google Calendar invite from a different account (Tentative response sent successfully) | Stable |
| 206 | `download_event_attachments` | Download file attachments from a calendar event | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 207 | `get_event_links` | Extract hyperlinks from an event's HTML description | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 208 | `assign_event_categories` | Add one or more categories to events, keeping any already present | `tests/smoke/tests/test_calendar_category_tagging.py` | OK (2026-09-08) — fixed by switching `_set_event_categories` to the bespoke `UpdateCalendarEvent` action captured from OWA's own web client; see "Update 2026-09-08 (continued) — fixed the category write-path" below. | Stable |
| 209 | `remove_event_categories` | Remove one or more categories from events, keeping any others present | `tests/smoke/tests/test_calendar_category_tagging.py` | OK (2026-09-08) — same fix as `assign_event_categories` above (shares `_set_event_categories`). | Stable |
| 210 | `find_events_by_category` | Find events tagged with a given category within a date range | `tests/smoke/tests/test_calendar_category_tagging.py` | OK (2026-09-08, re-verified) — pure `FindItem`+`CalendarView` read, unaffected by the write-path bug above; its date-range filtering had the same silent no-op bug as `get_calendar_events` (see below) and is now fixed by the same client-side filter. | Stable |

### Categories — [exchange_mcp/tools/categories.py](exchange_mcp/tools/categories.py) (4)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 301 | `list_categories` | List every category in the mailbox's master category list | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) | Stable |
| 302 | `create_category` | Create a new category in the master category list | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) | Stable |
| 303 | `rename_category` | Rename an existing category in the master category list | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) | Stable |
| 304 | `delete_category` | Delete a category from the master category list | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) | Stable |

### Directory — [exchange_mcp/tools/people.py](exchange_mcp/tools/people.py) (1)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 401 | `find_person` | Search Active Directory (`ResolveNames`) for people by name/email/keyword | `tests/smoke/tests/test_find_person.py` | KO (2026-09-09, re-verified) — `ResolveNames` throws a server-side `System.NullReferenceException` on this tenant (confirmed via `x-owa-error`/`x-owaerrormessageid` response headers) regardless of `SearchScope` (`ActiveDirectory`/`ActiveDirectoryContacts`/`Contacts`), `ContactDataShape`, or query shape (email vs. plain name) — not fixable client-side. Also breaks `get_meeting_stats` (below), which resolves names the same way. Fixed a real bug found while diagnosing this: `OWAClient._to_json()` treated *any* non-JSON, non-HTML response body as session expiry, so this 500 was misreported as `"Session may have expired"` and silently forced a pointless extra re-login attempt on every call; it now raises with the real HTTP status and `x-owa-error` detail instead, and only 401/440/HTML responses are treated as session expiry. | Dev |

### Folders — [exchange_mcp/tools/folders.py](exchange_mcp/tools/folders.py) (7)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 501 | `check_session` | Lightweight auth check (`FindFolder` on inbox) — reports mailbox name + unread count when the backend's response includes them (omitted on the modern OAuth/Bearer backend, which never returns `ParentFolder`) | `tests/smoke/tests/test_check_session.py` | OK (2026-09-07) | Stable |
| 502 | `get_folders` | List mail folders (shallow or recursive) with counts | `tests/smoke/tests/test_get_folders.py` | OK (2026-09-08) | Stable |
| 503 | `create_folder` | Create a new mail folder | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) | Stable |
| 504 | `rename_folder` | Rename an existing folder | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) | Stable |
| 505 | `empty_folder` | Empty all items from a folder | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) | Stable |
| 506 | `delete_folder` | Delete a mail folder | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) | Stable |
| 507 | `move_folder` | Move a folder under a different parent | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) | Stable |

### Availability — [exchange_mcp/tools/availability.py](exchange_mcp/tools/availability.py) (2)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 601 | `find_free_time` | Find free slots in your own calendar within working hours | `tests/smoke/tests/test_find_free_time.py` | OK (2026-09-08) | Stable |
| 602 | `find_meeting_time` | Find common free slots across multiple attendees (`GetUserAvailability`) | `tests/smoke/tests/test_find_meeting_time.py` | KO (2026-09-08) — `GetUserAvailability` returns a structured error body (`ErrorCode: 500, ExceptionName: NotImplementedException`) on this tenant; the action isn't implemented on this OWA backend at all. Also breaks `get_meeting_stats`/`get_meeting_contacts` (below), which share this call. Fixed a real bug found while diagnosing this: the error path read `body.get('FaultMessage', 'Unknown error')`, which doesn't fall back when the key is present-but-`null` (as here) — now falls back to `ExceptionName` so the real error surfaces instead of `{"error": null}`. | Dev |

### Analytics — [exchange_mcp/tools/analytics.py](exchange_mcp/tools/analytics.py) (2)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 701 | `get_meeting_stats` | Meeting-count statistics for one or more people over a date range | `tests/smoke/tests/test_get_meeting_stats.py` | KO (2026-09-08) — fails at the `ResolveNames` step (same `NullReferenceException` as `find_person`) before it ever reaches `GetUserAvailability`. Benefits from the same-day error-classification fix noted on `find_person` (401, above): now surfaces the real `NullReferenceException` instead of a misleading "session may have expired". | Dev |
| 702 | `get_meeting_contacts` | Weighted "who you meet with most" connection matrix from your own calendar | `tests/smoke/tests/test_get_meeting_contacts.py` | OK (2026-09-08), with caveats — doesn't call `ResolveNames`, so it doesn't hit that bug, but its sole data source (`GetUserAvailability`) is the same unimplemented action as `find_meeting_time`, so it always returns 0 meetings/0 contacts on this backend. Fixed a real bug found here: `_get_availability_events()` silently swallowed that failure (`except Exception: pass`) and never checked for an `ErrorCode` in a successfully-parsed error body either, so both this tool and `get_meeting_stats` were reporting a false-clean empty result instead of a diagnosable one. Now returns `(results, errors)` and both tools add a `"warnings"` field when `errors` is non-empty — verified live: a 30-day query now returns `"warnings": ["GetUserAvailability failed for [...]: NotImplementedException", ...]` instead of silently looking like "no meetings". [test_get_meeting_contacts.py](tests/smoke/tests/test_get_meeting_contacts.py)/[test_get_meeting_stats.py](tests/smoke/tests/test_get_meeting_stats.py) now include `warnings` in their recorded note when present, so a passing smoke-test run no longer hides this behind a bare `OK` — the underlying data is still empty on this backend (that part is unfixable here), but it's no longer silent about why. | Stable |

### Auth — [exchange_mcp/tools/auth.py](exchange_mcp/tools/auth.py) (1)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 801 | `login` | Credential setup + two-call, non-blocking 2FA login against the shared browser session | `tests/smoke/tests/test_login.py` (idempotent "already active" path only — see note below) | OK (2026-09-08) | Stable |

## 4. Gaps worth closing

- **Automated tests are live-mailbox smoke tests only, not unit tests.** `tests/smoke/`
  (added 2026-09-07) exercises each MCP tool end-to-end against a real OWA mailbox, one
  test module per tool. It does not cover pure logic in isolation — e.g.
  `_build_recipient_list` handling empty/whitespace addresses, or `folder_id_dict()`
  picking the right `__type` for a distinguished vs. opaque folder ID — both would be
  cheap to unit test without a live mailbox and remain a gap.
- **No live/manual QA log.** There's no record (changelog, issue tracker, etc.) of which
  of the 41 tools have actually been run against a real OWA mailbox since the
  browser-session rewrite. This document's "Manual QA / Status" column is a template for
  that log — fill it in as you verify each tool.
- **`BrowserSession`'s crash recovery doesn't cover a stuck profile lock.** Its documented
  recovery path (see CLAUDE.md's "Recovery" section) relaunches on the same profile directory
  and retries once if the browser process/context crashes. Observed during folder-tool testing
  (2026-09-08): after a long idle gap, an orphaned Chromium process tree from an earlier launch
  was still holding `.browser-profile`'s singleton lock, so every subsequent
  `launch_persistent_context` call — including the automatic one-retry recovery itself — hit
  the same lock and failed with "Target page, context or browser has been closed." Only a
  manual `taskkill /T /F` on the whole process tree (freeing the lock) followed by a fresh
  server start recovered it. **Reframed 2026-09-08**: this was originally logged as a rare,
  one-off idle-gap crash, but the same-day discovery that `server.py`'s lifespan ran fresh per
  client session (now fixed, see §1) means every single MCP connection up to that fix was
  independently launching Chromium against the same profile lock — i.e. lock contention was a
  structural risk on every multi-session run, not an edge case. The lifespan fix removes most of
  that exposure (one browser per process now, not per session), but the underlying gap — no
  stuck-lock detection/recovery — is still open and worth hardening later.
- **Cosmetic message bug in `respond_to_meeting`.** Its success message is built as
  `f"Meeting {response.lower()}ed"` ([calendar.py:1106](exchange_mcp/tools/calendar.py:1106)),
  which reads fine for "Accept"/"Decline" ("accepted"/"declined") but produces
  "tentativeed" for a Tentative response. Purely cosmetic — `success`/`response` fields
  are correct — but worth a one-line fix (e.g. an explicit map) next time that function is
  touched.
