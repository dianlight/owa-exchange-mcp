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
| `exchange_mcp/auth.py` | Login glue rewritten against `BrowserSession` — **deleted 2026-09-10**, see that update below |
| `login.py` | Rewritten for browser-based 2FA login — **deleted 2026-09-10**, see that update below |
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
*(The credential half of this is superseded by the 2026-09-10 update below — there is no
master password or stored credential to decrypt anymore. The pending-task bug it found is
still fixed, and the "move the profile aside and confirm the server comes up with no
session" technique is still exactly how to test the interactive path.)*
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

**Update 2026-09-09 — new Copilot tool module added (5 tools, discovery spike pending).**
Added `exchange_mcp/tools/copilot.py`: `ask_copilot` (generic delegator) plus 4
task-specific tools (`summarize_email_thread`, `draft_reply_with_copilot`,
`coach_draft`, `meeting_prep`) that all funnel through it. Unlike every other
tool module, this one has no documented OWA/EWS action to call against — there
is no Copilot API — so it drives Copilot's own chat pane inside the modern
Outlook web client via Playwright, the same way the project already drives the
real OWA web UI for `_set_event_categories` (#208/#209) when no API exists.
New `BrowserSession` methods (`_async_copilot_locate_pane`/`_open_pane`/
`_submit`/`_wait_and_read`/`_async_copilot_ask`/`copilot_ask()` in
[browser_session.py](exchange_mcp/browser_session.py)) open/locate the pane via
role-based Playwright locators (Fluent UI ARIA conventions), submit the prompt,
and poll `inner_text()` until it stabilizes and no "Stop generating"-style
control is visible, as a generation-complete heuristic. `OWAClient.ask_copilot()`/
`_copilot_item_url()` ([owa_client.py](exchange_mcp/owa_client.py)) wrap that
with the standard retry-once-on-session-expiry idiom and a best-effort deep-link
to ground the question against a specific email/event (`item_id`) — a failed
navigation there falls through to an ungrounded ask instead of raising, since
the deep-link URL shape is also unconfirmed.

Copilot only exists on the modern OAuth/Bearer backend ("new Outlook"), not
classic canary-cookie OWA, so every tool requires `BrowserSession.auth_mode ==
"bearer"` and raises the already-existing `BearerModeRequiredError` (added
alongside `find_people`/`post_substrate` for the same "modern-backend-only"
condition) otherwise. A separate new `CopilotUnavailableError` represents
Copilot's own rate-limit/capacity condition, distinct from session expiry.

**All of this is unverified against a live Copilot pane** — there is no
documented DOM/API reference to build against, so every selector, the
generation-complete heuristic, and the deep-link URL shape are best-guess
placeholders pending a live "discovery spike" (`--show-browser` inspection of
the real Copilot chat pane), explicitly flagged as provisional in the code's
own docstrings/comments. That spike was deferred in this session to avoid
colliding with another concurrently active session's use of the shared browser
profile/dev port, and has not yet run — see the `Pending` rows below and the
gap noted in §4.

`

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
from real usage: every attempt to move an email into any of several subfolders nested under
a custom top-level folder (e.g. `Projects/ClientFolder`) failed with "Folder not found," trying
both the full path and the bare folder name (10 attempts, all failed) — consistent with an
earlier failure moving into a folder nested one level under Inbox (e.g. `Inbox/Triage`). Root
cause: `get_folder_id()` ([owa_client.py](exchange_mcp/owa_client.py)) only ran a single
**Shallow** `FindFolder` rooted at `msgfolderroot` with a literal `DisplayName` match, so it
could see direct children of `msgfolderroot` and nothing else — a folder nested under another
custom folder, or under a distinguished folder like Inbox, was invisible to it regardless of
whether the caller passed the bare name or a `/`-delimited path (no path-splitting logic
existed at all; a literal string like `"Projects/ClientFolder"` could never equal a
single-segment `DisplayName`). Confirmed live against the real mailbox structure that this
class of bug was real and that a naive Deep-search-by-bare-name fallback would be unsafe: the
mailbox has two similarly-named top-level folders differing only by spacing, so resolving by
bare name alone (ignoring position in the hierarchy) risks landing in the wrong one.

Fixed by adding `_resolve_folder_path()`/`_find_child_folder_id()` to `get_folder_id()`: a
`/`-delimited path is now walked one Shallow `FindFolder` per segment, starting from a
distinguished folder (e.g. `Inbox`) when the first segment names one, otherwise from
`msgfolderroot`, resolving each subsequent segment as a child of the previous. This is
additive — bare single-segment names (the only other call shape used anywhere in the
codebase, by `email.py`/`calendar.py`/`analytics.py`/`availability.py`) are unaffected. New
regression test [test_move_email_nested_folder.py](tests/smoke/tests/test_move_email_nested_folder.py)
reproduces both bug shapes from the report against disposable, uniquely-tagged folders (a
custom folder nested under another custom folder, and a folder nested under the Inbox
distinguished folder) rather than the real folders from the report; both cases verified
live end-to-end (move + confirm landed at the nested path), passing after the fix.

**Update 2026-09-09 — found a working replacement for the broken `ResolveNames` path
(`find_person` #401, `get_meeting_stats` #701).** Investigated whether
`outlook.cloud.microsoft/people` (Microsoft's People app for this "new Outlook"
tenant) avoids the server-side `NullReferenceException` that makes `ResolveNames`
permanently unusable here, by sniffing its own network traffic. It does: the People
app's search box never calls `ResolveNames`/EWS at all — it POSTs to
`/search/api/v1/suggestions` (`domain=People`), a Microsoft Substrate Search endpoint
completely outside the `/owa/service.svc` EWS surface, and its contact-card expansion
uses `/PeopleGraphVx/v1.0/peopleLookup`. Decoding the captured bearer token's `aud`
claim confirmed both endpoints accept the *same* OAuth token
(`aud=https://outlook.office.com`) already captured by
`BrowserSession._async_capture_bearer_context()` for Mail actions, so no new auth
flow was needed, just a new transport path.

Added `BrowserSession._async_post_substrate()`/`post_substrate()`
([browser_session.py](exchange_mcp/browser_session.py)) to POST to an arbitrary path
on the bearer-mode origin (reusing `_async_execute_on_anchor()`'s anchor-page fetch,
since this "new Outlook" SPA has the same CDP response-body-retrieval failure on
these endpoints as on `/owa/service.svc`), raising a new `BearerModeRequiredError`
when the session is in classic canary mode (this surface doesn't exist there — e.g.
on-prem Exchange). `OWAClient.find_people()` builds the `/search/api/v1/suggestions`
request and calls it. `find_person` ([people.py](exchange_mcp/tools/people.py)) now
tries `find_people()` first and only falls back to `resolve_names()`
(`BearerModeRequiredError` is the specific, expected signal to fall back — any other
exception surfaces as a real error) — so classic/on-prem OWA, where `ResolveNames`
isn't broken, is unaffected. `get_meeting_stats`'s `_resolve_to_email()`
([analytics.py](exchange_mcp/tools/analytics.py)) got the same fallback.

Verified live end-to-end through the actual MCP tool call (dev server on :8765,
`test_find_person.py`/`test_get_meeting_stats.py`): `find_person("lucio.tarantino@unipol.it")`
now returns real directory data (name, email, job title, department, company, office,
phone, alias) instead of a guaranteed 500, and `get_meeting_stats` now resolves the
name/email correctly (the `GetUserAvailability` 500 it also reports is the
pre-existing, unrelated `find_meeting_time` #602 bug, not this one). The substrate
suggestions response doesn't carry manager/direct-reports/postal-address data at all
(unlike a fully-populated `ResolveNames` `Contact`), so those three fields stay empty
on this path — a real gap versus the old (theoretical, since it never actually
returned anything) `ResolveNames` shape, not a regression.

**Update 2026-09-09 — found a working replacement for the broken `GetUserAvailability`
path (`find_meeting_time` #602, and by extension `get_meeting_stats` #701/
`get_meeting_contacts` #702).** Investigated whether the modern Outlook Scheduling
Assistant UI itself calls `GetUserAvailability` for cross-mailbox free/busy, by driving
it manually while capturing network traffic. It doesn't: it calls a `GetSchedule`
GraphQL operation on `outlookgatewayb2/graphql` — the same bearer-mode gateway already
used for `find_people`'s substrate search — and a direct diagnostic call confirmed it
returns real, correct free/busy data (`error: null`) on this tenant, where
`GetUserAvailability` has always thrown `NotImplementedException` regardless of
`RequestedView`.

Added `OWAClient.get_schedule()` ([owa_client.py](exchange_mcp/owa_client.py)): builds
the `GetSchedule` GraphQL payload, POSTs it via `request_substrate()`, and returns one
dict per requested mailbox (`availability_view` — the same 0=Free/1=Tentative/2=Busy/
3=OOF/4=WorkingElsewhere per-interval digit encoding as `GetUserAvailability`'s
`MergedFreeBusy`, so the existing `_parse_freebusy_string()` parser applies unchanged —
plus `events`/`error`). Required fixing a latent URL-joining bug in
`BrowserSession._async_post_substrate()` ([browser_session.py](exchange_mcp/browser_session.py)):
it unconditionally assumed the path already contained a `?`, which broke on
`get_schedule()`'s bare-path call (unlike `find_people`'s, which includes its own query
string) — fixed with proper `?`/`&` separator logic, verified this didn't regress
`find_people`.

Wired `get_schedule()` into `find_meeting_time` ([availability.py](exchange_mcp/tools/availability.py))
and into the shared `_get_availability_events()` helpers used by `find_free_time`
(same file) and by `get_meeting_stats`/`get_meeting_contacts`
([analytics.py](exchange_mcp/tools/analytics.py)), each falling back to the legacy
`GetUserAvailability` EWS action only on `BearerModeRequiredError` (classic/on-prem
OWA, where the bearer-only substrate surface doesn't exist) — the same
try-modern-then-fall-back-to-EWS idiom already used for `find_people`/
`_resolve_to_email()`.

One real bug caught during live verification: `GetSchedule`'s `scheduleItems[].startTime/
endTime` come back UTC-aware (`Z` suffix) regardless of the requested `tz_id` — unlike
`availabilityView`/`workingHours`, which come back wall-clock in the requested zone — an
undocumented API inconsistency. `test_find_free_time.py` caught it as "can't compare
offset-naive and offset-aware datetimes"; fixed by stripping `tzinfo` in
`_parse_schedule_dt()`, matching the codebase's existing convention of treating these
timestamps as naive rather than doing a real timezone conversion.

Verified live against a dev server (`test_find_meeting_time.py`, `test_find_free_time.py`,
`test_get_meeting_stats.py`, `test_get_meeting_contacts.py`, `test_find_person.py` — the
last as a regression check on the shared substrate transport): all now return real
free/busy data instead of the `NotImplementedException` fault. `KNOWN_BUGGY_TOOLS` in
[server.py](exchange_mcp/server.py) is now empty — `find_meeting_time` was its only entry.

**Update 2026-09-10 — authentication rebuilt around the browser profile; credential
storage and `login.py` removed.** The server used to hold credentials: `login.py --setup`
encrypted a username/password with a master password, and `EXCHANGE_MASTER_PASSWORD`
let the server decrypt them at startup and drive the Entra ID sign-in form itself. That
whole mechanism is gone. Deleted: `login.py`, `exchange_mcp/auth.py`, the `cryptography`
dependency, `EXCHANGE_MASTER_PASSWORD`, and the credential-submitting login flow
(`BrowserSession._async_run_login_flow`). `.credentials.enc` / `.salt` are no longer read
by anything and can be deleted.

What replaces it, in [browser_session.py](exchange_mcp/browser_session.py) and
[server.py](exchange_mcp/server.py) (full contract in CLAUDE.md's "Authentication"
section):

1. **Profile-first.** `default_profile_dir()` resolves to `<repo>/.browser-profile` in a
   source checkout (detected by `pyproject.toml` next to the package) and
   `~/owa-mcp/.browser-profile` for an installed package — writing a browser profile into
   site-packages is wrong and often impossible. `EXCHANGE_BROWSER_PROFILE_DIR` still
   overrides. Startup reuses the directory if it exists and creates it if not, reporting
   which.
2. **Silent if possible.** `has_active_session()` accepts live OWA cookies, Microsoft's
   "stay signed in" cookie, or an SSO session the modern SPA can still mint a Bearer token
   from. If any of those holds, the server serves without showing anything.
3. **Interactive otherwise.** `interactive_login()` relaunches the context *visible*
   (`_async_relaunch(headless=False)`), parks it on the OWA sign-in page, and polls until a
   session appears or `EXCHANGE_LOGIN_TIMEOUT` (default 300s) expires. The user signs in;
   nothing is typed for them, so any 2FA method the deployment uses works unchanged.
4. **Never fatal.** If nobody completes the sign-in the server keeps serving; tools report
   `authorization_required` + `reason` + `remediation`, and the `login` tool reopens the
   window on demand. A stdio server is routinely spawned while the user is away, so exiting
   for that reason would be worse than waiting to be asked.

`auth_errors.py` survives with a narrower job: it no longer decides whether to keep
retrying (there is no credential to retry) — it explains a *timed-out* sign-in, running
once at the end, never mid-flow. Bailing out early would be wrong now: someone is sitting
in front of that window and can retype a password or re-approve a push themselves.

This supersedes an earlier same-day change that hardened the *credential* path (hard-stop
on a fatal login with exit code 78, and a known-bad-credentials latch to avoid AD smart
lockout). Both existed to make replaying a stored password safe; with no stored password
left, both were removed rather than left as dead code. The `AuthenticationRequiredError` /
`authorization_required` / `remediation` reporting it introduced is what remains, and one
bug it found stays fixed: `login.py`'s credential helpers printed to **stdout**, which is
the stdio transport's JSON-RPC stream when they ran inside the server.

Verified live against a throwaway profile directory: "creating new" on first start,
"reusing existing" on the second, an unauthenticated profile relaunching visible
(`headless` observed flipping to `False`) and waiting, and the timeout path leaving the
server up instead of exiting. [tests/unit/test_auth_errors.py](tests/unit/test_auth_errors.py)
covers the reason tables, the false-positive guards, and both branches of
`default_profile_dir()`. Not verified: an actual completed interactive sign-in (needs a
human at a real OWA window) — `login` #801 is `Pending` on that.

**Update 2026-09-10 (continued) — startup never ran under `--transport http`; startup
logging added.** Reported immediately after the change above: starting the server produced
no profile directory and no sign-in window at all. Root cause was a pre-existing structural
bug that the new auth flow made visible. `_startup` was only reachable through
`app_lifespan`, and under `--transport http` the mcp SDK runs the MCP lifespan *per client
session* — the very reason `_get_shared_client` was a module-level singleton in the first
place. So with no client connected, nothing ran: no browser, no profile, no window. (Under
stdio the client spawns the process and connects immediately, which is why it worked there
and the gap went unnoticed.) Reproduced by starting an http server on an isolated port and
profile with nothing connecting: only uvicorn's own log lines appeared, and the profile
directory was never created.

Fixed by making startup transport-independent:
- `main()` now calls `_ensure_started()` before `mcp.run()`, and `app_lifespan` still calls
  it (idempotent) so an embedder serving `mcp` directly behaves the same.
- `_startup` moved from an asyncio task to a `threading.Thread`. Under http there is no
  event loop yet at that point, and every `BrowserSession` method is already synchronous —
  and a thread keeps the port opening (or the stdio handshake) from waiting on a
  human-paced sign-in. That last part matters concretely: `server_manager.py` gives the
  server 90s to start listening, well short of the 300s login window.
- `_shared_state_lock` became a `threading.Lock`. It is now touched from `main()`'s bare
  thread *and* from a transport's event loop, and an `asyncio.Lock` binds itself to the
  first loop that uses it and rejects every other one.

Also added, as requested: a startup banner (version, OWA URL, resolved profile dir, *why*
it resolved that way, whether it already existed, headless vs. visible) followed by an
explicit `Auth status: AUTHENTICATED / NOT AUTHENTICATED / UNKNOWN` line with reason and
remediation. All of it via `_log()` to stderr. The "why" line exists because of the other
half of the same report — no `~/owa-mcp/` appeared. That was correct behavior, not a bug:
`pip install -e .` resolves `exchange_mcp` back into the repo, so `is_source_checkout()` is
true and the profile stays at `<repo>/.browser-profile` (which already existed and was
signed in). Invisible before; stated outright now.

`exchange_mcp.__version__` is now the single source of truth, with `pyproject.toml` reading
it via `[tool.setuptools.dynamic]`. An editable install doesn't refresh its metadata when
the tree changes — `importlib.metadata.version()` was reporting `2.0.0b0` against a
`2.0.0b1` tree — and a banner that misreports its own version is worse than no banner.

Verified: http server with no client now prints the banner, launches the browser, creates
the profile directory and opens the sign-in window, while uvicorn still starts listening
immediately; stdio prints the same banner; `pip wheel .` builds `2.0.0b1` from the dynamic
version; and the installed-package branch was confirmed by installing that wheel into a
throwaway venv and resolving `default_profile_dir()` from outside the repo →
`~/owa-mcp/.browser-profile`.

## 2. How to read the table

- **ID** — a permanent 3-digit identifier: digit 1 is the module number (fixed per
  module, see the module list below), digits 2-3 are the tool's sequence number within
  that module. Once assigned, a tool's ID never changes or gets reused, even if the
  table is reordered or tools are added/removed elsewhere — see CLAUDE.md's "Maintaining
  PROJECT_STATUS.md" section for the assignment rule. Module numbers: 1 Email, 2
  Calendar, 3 Categories, 4 Directory, 5 Folders, 6 Availability, 7 Analytics, 8 Auth,
  9 Copilot.
- **Automated test** — the test module(s) covering the row, or `None`. Coverage is
  `tests/smoke/` (live-mailbox, end-to-end, one module per tool) plus `tests/unit/`
  (pure logic, no mailbox). There is still no CI config — everything is run by hand.
- **Manual QA / Status** — whether the tool has actually been exercised against a real
  OWA mailbox since the browser-session rewrite, and the observed result. I have not run
  any of these tools myself in this session (that would require a live `EXCHANGE_OWA_URL`,
  stored credentials, and — for first login — your 2FA approval), so every row defaults to
  **`Pending`** unless you tell me otherwise. Update this column as you validate each tool;
  use `OK` / `KO` once you have an actual result, and add a one-line note (error text,
  date) for any `KO`.
- **Stability** — `Stable` unless the tool has a confirmed, unfixable server-side fault,
  in which case it's `Dev`. None currently qualify — `find_meeting_time` #602 was the
  only `Dev`-tagged tool, fixed 2026-09-09 by switching to the `GetSchedule` GraphQL
  operation (see the update above); `GetUserAvailability` itself is still unimplemented
  on this tenant, but no tool depends on it exclusively anymore. This is a narrower bar than `KO`/`Pending`:
  a tool that degrades gracefully instead of failing (e.g. `get_meeting_contacts` #702,
  which returns an empty result plus a `warnings` field rather than erroring) stays
  `Stable`, and an untested (`Pending`) tool also stays `Stable` by default — only move a
  tool to `Dev` once it's a confirmed, currently-unfixable failure. `Dev`-tagged tools are
  exactly the set in `KNOWN_BUGGY_TOOLS` in [server.py](exchange_mcp/server.py), which the
  `--stable` CLI flag / `EXCHANGE_MCP_STABLE` env var excludes from the MCP tool listing at
  startup — keep the two in sync (see CLAUDE.md's "Maintaining PROJECT_STATUS.md" section).

## 3. Tool inventory (48 tools across 9 modules)

### Email — [exchange_mcp/tools/email.py](exchange_mcp/tools/email.py) (15)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 101 | `get_emails` | List emails from a folder, grouped by conversation/thread, with unread/pagination filters; each row includes `flag_status`, and `body_error` when `include_body=True` could not fetch that row | `tests/smoke/tests/test_get_emails.py`, `tests/smoke/tests/test_email_flag.py`, `tests/smoke/tests/test_unfetchable_item_resilience.py` | OK (2026-09-10) — `flag_status` on every row; `include_body=True` now degrades individual unfetchable rows instead of failing the whole page (verified 10 rows / 8 bodies / 2 degraded) | Stable |
| 102 | `get_email` | Get a single email's full body, recipients, attachments, and `flag_status` (follow-up flag) | `tests/smoke/tests/test_get_email_detail.py`, `tests/smoke/tests/test_email_flag.py` | OK (2026-09-10) — `flag_status` verified round-tripping all three states. Fails for the handful of messages OWA cannot serialise (nothing to degrade to for a single item); now returns an explanatory `hint`, see §4. `test_get_email_detail` is flaky when it happens to pick one. | Stable |
| 103 | `send_email` | Send a new email (to/cc/bcc, HTML or plain text) | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 104 | `reply_email` | Reply (or reply-all) to an email | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 105 | `forward_email` | Forward an email to new recipients | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 106 | `mark_email_read` | Mark one or more emails read/unread | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 107 | `move_email` | Move one or more emails to another folder; `target_folder` accepts a bare name (direct child of `msgfolderroot`/a distinguished folder) or a `/`-delimited path for folders nested deeper (e.g. `Projects/ClientFolder`, `Inbox/Triage`) | `tests/smoke/tests/test_email_lifecycle.py`, `tests/smoke/tests/test_move_email_nested_folder.py` | OK (2026-09-09, re-verified) — see "Update 2026-09-09 — move_email couldn't resolve nested destination folders" below | Stable |
| 108 | `delete_email` | Delete (soft or permanent) one or more emails | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 109 | `download_attachments` | Download all file attachments from an email to disk | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) | Stable |
| 110 | `get_email_links` | Extract hyperlinks from an email's HTML body | `tests/smoke/tests/test_get_email_detail.py` | OK (2026-09-07) | Stable |
| 111 | `assign_email_categories` | Add one or more categories to emails, keeping any already present; reports `updated_count`/`failed_count`/`failed` and skips items whose details can't be read | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-10) — per-item resilience verified (`tests/smoke/tests/test_unfetchable_item_resilience.py`) | Stable |
| 112 | `remove_email_categories` | Remove one or more categories from emails, keeping any others present; reports `updated_count`/`failed_count`/`failed` and skips items whose details can't be read | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-10) — per-item resilience verified (`tests/smoke/tests/test_unfetchable_item_resilience.py`) | Stable |
| 113 | `find_emails_by_category` | Find email conversations tagged with a given category | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-08) | Stable |
| 114 | `search_emails` | Full-text search for emails, scoped to one folder or the whole mailbox. Tries EWS `FindItem`/`QueryString` (AQS syntax: `subject:`, `from:`, `body:`, `received:`, etc.) first, then transparently falls back to a client-side scan (reduced keyword subset: bare terms, `subject:`, `from:`, `category:`, `isread:`, `hasattachment:`) — this tenant's content index never returns AQS results, and `FindItem`'s `Traversal:"Deep"` is unsupported outright, so `search_all_folders` enumerates folders via `FindFolder`/`Deep` (like `get_folders`) and searches each one `Shallow` | `tests/smoke/tests/test_search_emails.py` | OK (2026-09-09) — single-folder AQS-empty + fallback, and `search_all_folders=True` across folders, both verified live; fixed `folder_id` always returning empty (`FindItem`'s `AdditionalProperties` needs the namespaced `item:ParentFolderId` FieldURI, not bare `ParentFolderId`) — re-verified non-empty `folder_id` live via the fallback path | Stable |
| 115 | `set_email_flag` | Set the follow-up flag (`NotFlagged`/`Flagged`/`Complete`) on one or more emails, via `UpdateItem`/`SetItemField` on `item:Flag` | `tests/smoke/tests/test_email_flag.py` | OK (2026-09-10) — all three states written and read back successfully. The wire encoding is fussy: only `FieldURI: "item:Flag"` paired with `__type: "FlagType:#Exchange"` is accepted; `message:Flag` (either `__type`) returns "Invalid argument used to call method UpdateItem", and PidLidFlagStatus 0x8530 as an ExtendedFieldURI is rejected in every spelling tried. Invalid `flag_status` rejected client-side. | Stable |

### Calendar — [exchange_mcp/tools/calendar.py](exchange_mcp/tools/calendar.py) (11)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 201 | `get_calendar_events` | List events in a date range, including each event's `categories`. By default a recurring series appears once, as its master item; `expand_recurrences=True` additionally synthesizes one entry per occurrence client-side (marked `is_synthesized_occurrence`, empty `item_id` — see §4) | `tests/smoke/tests/test_get_calendar_events.py`, `tests/smoke/tests/test_calendar_event_detail.py`, `tests/smoke/tests/test_recurrence_expansion.py`, `tests/unit/test_recurrence_expansion.py` | OK (2026-09-10) — `categories` verified round-tripping a real tag; `expand_recurrences` verified live (46 synthesized occurrences over 14 days, all in-window, all `item_id`-less, no duplicated masters, correct time-of-day) and all 127 recurring series in this mailbox expand, relative patterns included | Stable |
| 202 | `create_meeting` | Create a meeting with attendees, location, reminder, sensitivity | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 203 | `update_meeting` | Update a meeting (implemented as cancel + recreate — OWA JSON API has no reliable `UpdateItem` for calendar items) | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 204 | `cancel_meeting` | Cancel a meeting and notify attendees (soft-delete only — moves to Deleted Items, no permanent-delete option) | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 205 | `respond_to_meeting` | Accept / decline / tentatively accept a meeting invite | None — self-invite produces no meeting-request email to respond to (confirmed 2026-09-08; Exchange doesn't ask an organizer to accept their own invite), so this can't be covered by a self-contained automated test | OK (2026-09-08, manual) — verified against a real incoming Google Calendar invite from a different account (Tentative response sent successfully) | Stable |
| 206 | `download_event_attachments` | Download file attachments from a calendar event | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 207 | `get_event_links` | Extract hyperlinks from an event's HTML description | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) | Stable |
| 208 | `assign_event_categories` | Add one or more categories to events, keeping any already present | `tests/smoke/tests/test_calendar_category_tagging.py` | OK (2026-09-08) — fixed by switching `_set_event_categories` to the bespoke `UpdateCalendarEvent` action captured from OWA's own web client; see "Update 2026-09-08 (continued) — fixed the category write-path" below. | Stable |
| 209 | `remove_event_categories` | Remove one or more categories from events, keeping any others present | `tests/smoke/tests/test_calendar_category_tagging.py` | OK (2026-09-08) — same fix as `assign_event_categories` above (shares `_set_event_categories`). | Stable |
| 210 | `find_events_by_category` | Find events tagged with a given category within a date range | `tests/smoke/tests/test_calendar_category_tagging.py` | OK (2026-09-08, re-verified) — pure `FindItem`+`CalendarView` read, unaffected by the write-path bug above; its date-range filtering had the same silent no-op bug as `get_calendar_events` (see below) and is now fixed by the same client-side filter. | Stable |
| 211 | `get_calendar_event` | Get full details for a single calendar event by ItemId (subject, start/end, location, body, organizer, attendees, categories, change_key, recurrence) | `tests/smoke/tests/test_calendar_event_detail.py` | OK (2026-09-10) — verified against a disposable tagged event: subject/start/end/categories/change_key all returned, assigned category round-tripped, bogus item_id rejected cleanly | Stable |

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
| 401 | `find_person` | Search the directory for people by name/email/keyword — Substrate Search (`/search/api/v1/suggestions`) on the modern Outlook backend, falling back to `ResolveNames` on classic OWA | `tests/smoke/tests/test_find_person.py` | OK (2026-09-09) — `ResolveNames` still throws a server-side `System.NullReferenceException` on this tenant regardless of `SearchScope`/`ContactDataShape`/query shape (not fixable client-side; see the 2026-09-09 update above), but `outlook.cloud.microsoft/people`'s own search box doesn't use `ResolveNames` at all — it calls the Substrate Search REST API, which works on this tenant and returns real directory data over the same bearer token already used for Mail. `find_person` now tries that path first and only falls back to `ResolveNames` when the session is in classic canary-cookie auth mode (on-prem, or a cloud tenant not yet on the modern backend). Also breaks `get_meeting_stats`'s name-resolution step the same way — see #701, below — fixed by the same fallback. Caveat: the Substrate Search response has no manager/direct-reports/postal-address fields, so those stay empty on this path (only the `ResolveNames` fallback can populate them). | Stable |

### Folders — [exchange_mcp/tools/folders.py](exchange_mcp/tools/folders.py) (7)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 501 | `check_session` | Lightweight auth check (`FindFolder` on inbox) — reports mailbox name + unread count when the backend's response includes them (omitted on the modern OAuth/Bearer backend, which never returns `ParentFolder`). An unauthenticated profile now comes back as `authorization_required` + `reason` + `remediation` rather than a generic error string, pointing the caller at the `login` tool (see the 2026-09-10 update below) | `tests/smoke/tests/test_check_session.py` | OK (2026-09-07) for the authenticated/generic-error paths; the new `authorization_required` branch is `Pending` | Stable |
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
| 602 | `find_meeting_time` | Find common free slots across multiple attendees — tries the modern-backend `GetSchedule` GraphQL operation first, falling back to EWS `GetUserAvailability` only on classic/on-prem OWA | `tests/smoke/tests/test_find_meeting_time.py` | OK (2026-09-09) — `GetUserAvailability` still returns `{ErrorCode: 500, ExceptionName: NotImplementedException}` on this tenant and is unfixable server-side, but the Scheduling Assistant UI's own `GetSchedule` operation works correctly here (confirmed via live network capture) and returns free/busy data in the same `MergedFreeBusy`-compatible encoding; see "Update 2026-09-09 — found a working replacement for the broken GetUserAvailability path" above. Also fixes `get_meeting_stats`/`get_meeting_contacts` (below), which share the underlying helper. A prior fix (2026-09-08) to the legacy fallback's error path (`FaultMessage`→`ExceptionName` when the former is present-but-`null`) still applies to that branch. | Stable |

### Analytics — [exchange_mcp/tools/analytics.py](exchange_mcp/tools/analytics.py) (2)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 701 | `get_meeting_stats` | Meeting-count statistics for one or more people over a date range | `tests/smoke/tests/test_get_meeting_stats.py` | OK (2026-09-09) — used to fail at the `ResolveNames` step (same `NullReferenceException` as `find_person` #401) before ever reaching availability data. `_resolve_to_email()` tries the Substrate Search path first (see #401, above) and falls back to `ResolveNames` only in classic canary-cookie auth mode. Its shared `_get_availability_events()` helper now tries `GetSchedule` first (see #602, above) and only falls back to the still-unimplemented `GetUserAvailability` on classic OWA, so this tool now returns real per-person stats without needing the `warnings` fallback in the common case — `warnings` remains for the classic-OWA/`GetUserAvailability`-failure path. | Stable |
| 702 | `get_meeting_contacts` | Weighted "who you meet with most" connection matrix from your own calendar | `tests/smoke/tests/test_get_meeting_contacts.py` | OK (2026-09-09) — doesn't call `ResolveNames`, so it doesn't hit that bug. Its sole data source (own-mailbox availability via the shared `_get_availability_events()` helper) now tries `GetSchedule` first (see #602, above) and returns real meeting/contact data instead of the empty result `GetUserAvailability` always produced on this tenant. A real bug fixed while diagnosing this originally (2026-09-08): `_get_availability_events()` silently swallowed the `GetUserAvailability` failure (`except Exception: pass`) and never checked for an `ErrorCode` in a successfully-parsed error body either, so both this tool and `get_meeting_stats` were reporting a false-clean empty result instead of a diagnosable one — it now returns `(results, errors)`, and both tools add a `"warnings"` field when `errors` is non-empty (still relevant on classic OWA, where the legacy fallback is the only option). [test_get_meeting_contacts.py](tests/smoke/tests/test_get_meeting_contacts.py)/[test_get_meeting_stats.py](tests/smoke/tests/test_get_meeting_stats.py) include `warnings` in their recorded note when present. | Stable |

### Auth — [exchange_mcp/tools/auth.py](exchange_mcp/tools/auth.py) (1)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 801 | `login` | Opens a **visible browser window** on the OWA sign-in page and waits for the user to sign in (two-call flow: first call opens the window, second reports the result). No credential arguments — `force` only. Failure responses carry `authorization_required` / `reason` / `remediation`. Completely rewritten 2026-09-10 (was: master-password + stored encrypted credentials + 2FA push wait) | `tests/smoke/tests/test_login.py` (idempotent "already active" path only — the interactive path needs a human at the window), `tests/unit/test_auth_errors.py` (reason tables, profile-dir resolution) | **Pending** — signature and behavior fully replaced 2026-09-10; the already-active short-circuit is unchanged in spirit but the interactive window path has not been driven end to end by a human yet | Stable |

### Copilot — [exchange_mcp/tools/copilot.py](exchange_mcp/tools/copilot.py) (5)

| ID | Tool | Description | Automated test | Manual QA / Status | Stability |
|---|---|---|---|---|---|
| 901 | `ask_copilot` | Generic delegator: sends a free-text prompt to Copilot's chat pane, optionally grounded against an email/event via a best-effort deep link | None | Pending — see "Update 2026-09-09 — new Copilot tool module added" above; DOM selectors are unverified placeholders pending a live discovery spike | Stable |
| 902 | `summarize_email_thread` | Ask Copilot to summarize an email thread and list action items | None | Pending — same caveats as #901 | Stable |
| 903 | `draft_reply_with_copilot` | Ask Copilot to draft a reply to an email per free-text instructions/tone; returns text only, doesn't send | None | Pending — same caveats as #901; whether Copilot's compose-time drafting needs a live reply window instead of the plain chat pane is also unconfirmed | Stable |
| 904 | `coach_draft` | Ask Copilot's compose coaching for feedback on a draft reply's tone/clarity | None | Pending — same caveats as #901; Copilot's "Coaching" affordance may live inside an in-progress compose window rather than the chat pane this tool drives, unconfirmed | Stable |
| 905 | `meeting_prep` | Ask Copilot to prepare a briefing for an upcoming meeting (context, documents, action items) | None | Pending — same caveats as #901 | Stable |

## 4. Gaps worth closing

- **`expand_recurrences`: all recurrence patterns in this mailbox now expand; the
  remaining limit is cost.** The `Recurrence` schema is confirmed (2026-09-10, across all 127
  series here) and documented in `_expand_recurrence_occurrences`: this backend nests the
  variant in a `__type` field under fixed `RecurrencePattern`/`RecurrenceRange` keys rather
  than using it as the key the way plain-EWS JSON does, and range dates are
  date-plus-offset with no time (`"2026-07-22+02:00"`), so the master's own `Start` supplies
  the time-of-day. Relative patterns ("2nd Wednesday of the month") were initially skipped
  and are **now implemented** — daily / weekly / absolute-monthly / absolute-yearly /
  relative-monthly / relative-yearly, i.e. **127 of 127 series expandable** (was 123).
  EWS's pseudo-days (`Day`/`Weekday`/`WeekendDay` in `DaysOfWeek`) are still not expanded:
  none occur here and each means something other than a plain weekday, so they degrade to
  no occurrences rather than inventing dates. Covered by
  `tests/unit/test_recurrence_expansion.py` (pure logic, exact dates, real payloads) and
  `tests/smoke/tests/test_recurrence_expansion.py` (live).
  **Still open — cost:** expansion is O(recurring masters in the whole folder), not
  O(events in the window), because a master's `Start` describes only its first occurrence
  so every master must be fetched to know whether it lands in the window: one `GetItem`
  each, ~20-30s for ~100 masters. Also note synthesized occurrences deliberately carry an
  **empty `item_id`** and cannot be passed to any mutating tool; act on the series via the
  master row.
- **The follow-up-flag write encoding is load-bearing and non-obvious.** Solved by
  elimination against a live mailbox 2026-09-10. Only `SetItemField` with
  `Path.FieldURI = "item:Flag"` **and** `Item.Flag.__type = "FlagType:#Exchange"` is
  accepted. Rejected: `message:Flag` with either `Flag:#Exchange` or `FlagType:#Exchange`
  ("Invalid argument used to call method UpdateItem"); and `PidLidFlagStatus`
  (PSETID_Common 0x8530) as an ExtendedFieldURI in every spelling tried
  (`PathToExtendedFieldType`/`ExtendedPropertyUri` x `DistinguishedPropertySetId`/literal
  `PropertySetId` GUID, plus a `PropertyTag` for PidTagFollowupIcon) — those return
  ErrorCode 500 or "the combination of extended property attributes is not valid". No
  extended-property write of any kind has ever succeeded against this backend, so treat
  `ExtendedFieldURI` as unavailable here rather than as a fallback.
  `tests/smoke/tests/test_email_flag.py` guards the working encoding.
- **Some messages cannot be fetched at all (server-side), now contained.** At least two
  messages in this Inbox make OWA's own `GetItem` throw
  `System.Runtime.Serialization.SerializationException` (HTTP 500) — both meeting-related
  items, and pre-existing/unrelated to any change here (ruled out as a `set_email_flag`
  side effect: one was already failing before any flag write succeeded, a message with
  three *successful* flag writes still reads fine, and one with only *failed* writes also
  still reads fine). There is no client-side fix for the fault itself. **Fixed 2026-09-10** is the
  collateral damage: `_try_get_item_details` now lets any per-item loop skip one bad item,
  so `get_emails(include_body=True)` returns the rest of the page with `body_error` set on
  just the affected rows (verified: `limit=10` returns 10 rows, 8 with bodies, 2 degraded),
  and `assign_email_categories`/`remove_email_categories` report
  `updated_count`/`failed_count`/`failed` instead of aborting mid-batch while keeping
  earlier writes. `get_email` on such an item still fails — there is nothing to degrade to
  for a single item — but now returns a `hint` saying the server, not the item_id or the
  session, is at fault. Guarded by `tests/smoke/tests/test_unfetchable_item_resilience.py`.
  Still open upstream: nothing this client can do about the serialization fault itself.
- **Automated tests are almost entirely live-mailbox smoke tests.** `tests/smoke/`
  exercises each MCP tool end-to-end against a real mailbox, one module per tool, so it
  cannot run in CI and cannot cover pure logic in isolation. `tests/unit/` is the
  exception and now holds two suites: `test_auth_errors` (sign-in failure diagnosis and
  profile-directory resolution) and `test_recurrence_expansion` (occurrence arithmetic for
  every pattern/range variant, exact dates, real captured payloads, malformed-payload
  degradation). Other cheap pure-logic targets remain uncovered: e.g.
  `_build_recipient_list` handling empty/whitespace addresses, or `folder_id_dict()`
  picking the right `__type` for a distinguished vs. opaque folder ID.
- **No live/manual QA log.** There's no record (changelog, issue tracker, etc.) of which
  of the 48 tools have actually been run against a real OWA mailbox since the
  browser-session rewrite. This document's "Manual QA / Status" column is a template for
  that log — fill it in as you verify each tool.
- **Copilot module (#901-905) needs a live discovery spike.** Every DOM selector,
  the generation-complete polling heuristic, and the item-grounding deep-link URL
  shape in `browser_session.py`'s Copilot section are best-guess placeholders — there
  is no documented Copilot API/DOM reference to build against. Needs a
  `--show-browser` session against a real Copilot chat pane to confirm/correct the
  selectors before any of the 5 rows above can move past `Pending`. Deferred in the
  session that added this module to avoid colliding with another concurrently
  active session's use of the shared browser profile/dev port.
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
