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
→ empty (verified gone) → permanently delete. The move-before-populate ordering isn't
arbitrary: `move_email`'s `target_folder` and `get_emails`'s `folder` argument both resolve a
folder *name* via a **Shallow** `FindFolder` search rooted at `msgfolderroot`
([owa_client.py](exchange_mcp/owa_client.py) `get_folder_id()`), so a folder nested one level
under Inbox is invisible to those lookups until `move_folder` promotes it to a direct child of
`msgfolderroot`.

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

The calendar-side per-item write tools do not: `assign_event_categories`/
`remove_event_categories` cannot persist a `Categories` change on this OWA build — see
their `KO` rows below. Getting here surfaced and fixed a real false-positive-success bug
along the way: OWA's `UpdateItem` action can return a top-level fault envelope
(`{"Body": {"ErrorCode": ..., "FaultMessage": ...}}`) with no `ResponseMessages` key at
all, which `OWAClient.extract_items()` silently treats as "no items, so no error" — every
existing `UpdateItem`-based tool that only checked per-item `ResponseClass == "Error"` was
exposed to this exact class of silent failure. Fixed centrally in
[owa_client.py](exchange_mcp/owa_client.py)'s `_to_json()`, which now raises immediately on
that fault-envelope shape, so any current or future caller gets a real error instead of a
false "success" — this is a general hardening, not specific to categories.
`find_events_by_category` (a pure `FindItem`+`CalendarView` read, unaffected by the write
bug) works correctly.

All 40 tools have now been exercised at least once against a real OWA mailbox.

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

## 3. Tool inventory (40 tools across 8 modules)

### Email — [exchange_mcp/tools/email.py](exchange_mcp/tools/email.py) (13)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `get_emails` | List emails from a folder, grouped by conversation/thread, with unread/pagination filters | Migrated | `tests/smoke/tests/test_get_emails.py` | OK (2026-09-07) |
| `get_email` | Get a single email's full body, recipients, and attachments | Migrated | `tests/smoke/tests/test_get_email_detail.py` | OK (2026-09-07) |
| `send_email` | Send a new email (to/cc/bcc, HTML or plain text) | Migrated | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) |
| `reply_email` | Reply (or reply-all) to an email | Migrated | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) |
| `forward_email` | Forward an email to new recipients | Migrated | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) |
| `mark_email_read` | Mark one or more emails read/unread | Migrated | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) |
| `move_email` | Move one or more emails to another folder | Migrated | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) |
| `delete_email` | Delete (soft or permanent) one or more emails | Migrated | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) |
| `download_attachments` | Download all file attachments from an email to disk | Migrated | `tests/smoke/tests/test_email_lifecycle.py` | OK (2026-09-07) |
| `get_email_links` | Extract hyperlinks from an email's HTML body | Migrated | `tests/smoke/tests/test_get_email_detail.py` | OK (2026-09-07) |
| `assign_email_categories` | Add one or more categories to emails, keeping any already present | Migrated | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-08) |
| `remove_email_categories` | Remove one or more categories from emails, keeping any others present | Migrated | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-08) |
| `find_emails_by_category` | Find email conversations tagged with a given category | Migrated | `tests/smoke/tests/test_email_category_tagging.py` | OK (2026-09-08) |

### Calendar — [exchange_mcp/tools/calendar.py](exchange_mcp/tools/calendar.py) (10)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `get_calendar_events` | List events in a date range; optional recurring-occurrence expansion | Migrated | `tests/smoke/tests/test_get_calendar_events.py` | OK (2026-09-08) |
| `create_meeting` | Create a meeting with attendees, location, reminder, sensitivity | Migrated | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) |
| `update_meeting` | Update a meeting (implemented as cancel + recreate — OWA JSON API has no reliable `UpdateItem` for calendar items) | Migrated | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) |
| `cancel_meeting` | Cancel a meeting and notify attendees (soft-delete only — moves to Deleted Items, no permanent-delete option) | Migrated | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) |
| `respond_to_meeting` | Accept / decline / tentatively accept a meeting invite | Migrated | None — self-invite produces no meeting-request email to respond to (confirmed 2026-09-08; Exchange doesn't ask an organizer to accept their own invite), so this can't be covered by a self-contained automated test | OK (2026-09-08, manual) — verified against a real incoming Google Calendar invite from a different account (Tentative response sent successfully) |
| `download_event_attachments` | Download file attachments from a calendar event | Migrated | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) |
| `get_event_links` | Extract hyperlinks from an event's HTML description | Migrated | `tests/smoke/tests/test_calendar_lifecycle.py` | OK (2026-09-08) |
| `assign_event_categories` | Add one or more categories to events, keeping any already present | Migrated | `tests/smoke/tests/test_calendar_category_tagging.py` | KO (2026-09-08) — `UpdateItem` on a `CalendarItem` always fails with `ErrorSendMeetingInvitationsOrCancellationsRequired` on this OWA build, even with the attribute sent at the exact documented position plus the `Specified` companion flag Microsoft's own EWS Managed API code sample sets alongside it. Ruled out: the attribute's value, the `Specified` flag, meeting vs. plain zero-attendee appointment, and a bespoke `UpdateCalendarEvent` action (which instead rejects the standard `ItemId` as malformed). Not fixable client-side without a captured example of OWA's own web client performing this action — see `_set_event_categories`'s docstring in [calendar.py](exchange_mcp/tools/calendar.py) for the full trail. |
| `remove_event_categories` | Remove one or more categories from events, keeping any others present | Migrated | `tests/smoke/tests/test_calendar_category_tagging.py` | KO (2026-09-08) — same root cause as `assign_event_categories` above (shares `_set_event_categories`). |
| `find_events_by_category` | Find events tagged with a given category | Migrated | `tests/smoke/tests/test_calendar_category_tagging.py` | OK (2026-09-08) — pure `FindItem`+`CalendarView` read, unaffected by the write-path bug above. |

### Categories — [exchange_mcp/tools/categories.py](exchange_mcp/tools/categories.py) (4)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `list_categories` | List every category in the mailbox's master category list | Migrated | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) |
| `create_category` | Create a new category in the master category list | Migrated | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) |
| `rename_category` | Rename an existing category in the master category list | Migrated | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) |
| `delete_category` | Delete a category from the master category list | Migrated | `tests/smoke/tests/test_category_lifecycle.py` | OK (2026-09-08) |

### Directory — [exchange_mcp/tools/people.py](exchange_mcp/tools/people.py) (1)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `find_person` | Search Active Directory (`ResolveNames`) for people by name/email/keyword | Migrated | `tests/smoke/tests/test_find_person.py` | KO (2026-09-08) — `ResolveNames` throws a server-side `System.NullReferenceException` on this tenant (confirmed via `x-owa-error`/`x-owaerrormessageid` response headers); not fixable client-side. Also breaks `get_meeting_stats` (below), which resolves names the same way. |

### Folders — [exchange_mcp/tools/folders.py](exchange_mcp/tools/folders.py) (7)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `check_session` | Lightweight auth check (`FindFolder` on inbox) — reports mailbox name + unread count when the backend's response includes them (omitted on the modern OAuth/Bearer backend, which never returns `ParentFolder`) | Migrated | `tests/smoke/tests/test_check_session.py` | OK (2026-09-07) |
| `get_folders` | List mail folders (shallow or recursive) with counts | Migrated | `tests/smoke/tests/test_get_folders.py` | OK (2026-09-08) |
| `create_folder` | Create a new mail folder | Migrated | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) |
| `rename_folder` | Rename an existing folder | Migrated | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) |
| `empty_folder` | Empty all items from a folder | Migrated | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) |
| `delete_folder` | Delete a mail folder | Migrated | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) |
| `move_folder` | Move a folder under a different parent | Migrated | `tests/smoke/tests/test_folder_lifecycle.py` | OK (2026-09-08) |

### Availability — [exchange_mcp/tools/availability.py](exchange_mcp/tools/availability.py) (2)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `find_free_time` | Find free slots in your own calendar within working hours | Migrated | `tests/smoke/tests/test_find_free_time.py` | OK (2026-09-08) |
| `find_meeting_time` | Find common free slots across multiple attendees (`GetUserAvailability`) | Migrated | `tests/smoke/tests/test_find_meeting_time.py` | KO (2026-09-08) — `GetUserAvailability` returns a structured error body (`ErrorCode: 500, ExceptionName: NotImplementedException`) on this tenant; the action isn't implemented on this OWA backend at all. Also breaks `get_meeting_stats`/`get_meeting_contacts` (below), which share this call. Fixed a real bug found while diagnosing this: the error path read `body.get('FaultMessage', 'Unknown error')`, which doesn't fall back when the key is present-but-`null` (as here) — now falls back to `ExceptionName` so the real error surfaces instead of `{"error": null}`. |

### Analytics — [exchange_mcp/tools/analytics.py](exchange_mcp/tools/analytics.py) (2)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `get_meeting_stats` | Meeting-count statistics for one or more people over a date range | Migrated | `tests/smoke/tests/test_get_meeting_stats.py` | KO (2026-09-08) — fails at the `ResolveNames` step (same `NullReferenceException` as `find_person`) before it ever reaches `GetUserAvailability`. |
| `get_meeting_contacts` | Weighted "who you meet with most" connection matrix from your own calendar | Migrated | `tests/smoke/tests/test_get_meeting_contacts.py` | OK (2026-09-08), with caveats — doesn't call `ResolveNames`, so it doesn't hit that bug, but its sole data source (`GetUserAvailability`) is the same unimplemented action as `find_meeting_time`, so it always returns 0 meetings/0 contacts on this backend. Fixed a real bug found here: `_get_availability_events()` silently swallowed that failure (`except Exception: pass`) and never checked for an `ErrorCode` in a successfully-parsed error body either, so both this tool and `get_meeting_stats` were reporting a false-clean empty result instead of a diagnosable one. Now returns `(results, errors)` and both tools add a `"warnings"` field when `errors` is non-empty — verified live: a 30-day query now returns `"warnings": ["GetUserAvailability failed for [...]: NotImplementedException", ...]` instead of silently looking like "no meetings". [test_get_meeting_contacts.py](tests/smoke/tests/test_get_meeting_contacts.py)/[test_get_meeting_stats.py](tests/smoke/tests/test_get_meeting_stats.py) now include `warnings` in their recorded note when present, so a passing smoke-test run no longer hides this behind a bare `OK` — the underlying data is still empty on this backend (that part is unfixable here), but it's no longer silent about why. |

### Auth — [exchange_mcp/tools/auth.py](exchange_mcp/tools/auth.py) (1)

| Tool | Description | Migration | Automated test | Manual QA / Status |
|---|---|---|---|---|
| `login` | Credential setup + two-call, non-blocking 2FA login against the shared browser session | Migrated | `tests/smoke/tests/test_login.py` (idempotent "already active" path only — see note below) | OK (2026-09-08) |

## 4. Gaps worth closing

- **Automated tests are live-mailbox smoke tests only, not unit tests.** `tests/smoke/`
  (added 2026-09-07) exercises each MCP tool end-to-end against a real OWA mailbox, one
  test module per tool. It does not cover pure logic in isolation — e.g.
  `_build_recipient_list` handling empty/whitespace addresses, or `folder_id_dict()`
  picking the right `__type` for a distinguished vs. opaque folder ID — both would be
  cheap to unit test without a live mailbox and remain a gap.
- **No live/manual QA log.** There's no record (changelog, issue tracker, etc.) of which
  of the 30 tools have actually been run against a real OWA mailbox since the
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
