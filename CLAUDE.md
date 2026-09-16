# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Exchange MCP is a Model Context Protocol server for any Microsoft Exchange / OWA (Outlook Web Access) deployment. It gives LLM agents full access to email, calendar, directory, availability, and meeting analytics over the standard OWA JSON API, plus Copilot delegation (chat-pane UI automation, modern Outlook backend only — no JSON API exists for it). Works with any on-premise or hosted Exchange server that exposes OWA.

## Configuration

The server requires one environment variable, plus optional ones for the browser session:

- `EXCHANGE_OWA_URL` — Base URL of the OWA instance (e.g. `https://owa.example.com`)
- `EXCHANGE_BROWSER_PROFILE_DIR` — (optional) Path to the persistent Chromium profile directory. Default comes from `browser_session.default_profile_dir()`: `<repo>/.browser-profile` in a source checkout (detected by `pyproject.toml` next to the package), `~/owa-mcp/.browser-profile` for an installed package — site-packages is the wrong place, and often unwritable, for a browser profile.
- `EXCHANGE_LOGIN_TIMEOUT` — (optional) Seconds the interactive sign-in window waits for the user before giving up. Default 300 (`LOGIN_WINDOW_SECONDS` in `server.py`).
- `EXCHANGE_HEADLESS` — (optional) Set to `false`/`0` to run the browser with a visible window. Same effect as the `--show-browser` CLI flag (which takes precedence).
- `EXCHANGE_MCP_TRANSPORT` — (optional) `stdio` (default) or `http`. Same effect as `--transport`.
- `EXCHANGE_MCP_HOST` / `EXCHANGE_MCP_PORT` — (optional) Bind address for `--transport http`. Default `127.0.0.1:8765` — never bind non-loopback, the MCP endpoint has no auth of its own.
- `EXCHANGE_MCP_STABLE` — (optional) Set to `true`/`1`/`yes` to exclude known-buggy tools (`KNOWN_BUGGY_TOOLS` in `server.py`, kept in sync with the KO rows / Stability column in PROJECT_STATUS.md) from the MCP tool listing at startup, instead of exposing them to fail at call time. Same effect as the `--stable` CLI flag.
- `EXCHANGE_TIMEZONE` — (optional) The mailbox's timezone, as a Windows id (`W. Europe Standard Time`) or an IANA one (`Europe/Rome`). Used by `availability_frame` to put UTC busy periods on the local working-hours grid; unset, it asks `OWAClient.mailbox_timezone()` where that exists and otherwise doesn't shift anything (and says so in the tool's own output). An IANA id always resolves, so this is the escape hatch for a Windows id the table doesn't know.
- `EXCHANGE_DISCOVERY_DIR` — (optional) Where capability-discovery captures are written. Default comes from `discovery_session.default_discovery_dir()` and mirrors the profile-dir split: `<repo>/.discovery-sessions` in a source checkout, `~/owa-mcp/discovery-sessions` for an installed package. Gitignored — a capture holds real mailbox metadata (see "Capability discovery" below).

**Version**: `exchange_mcp/__version__` is the single source of truth — `pyproject.toml` reads it via `[tool.setuptools.dynamic] version = {attr = ...}`, and the startup banner prints it. Don't hardcode a version in `pyproject.toml`: an editable install doesn't refresh its metadata when the tree changes, so `importlib.metadata.version()` goes stale and a banner that misreports its own version is worse than no banner. `server.json`'s two `version` fields still have to be bumped by hand (it's a published registry manifest and can't read Python attributes).

Any variable above can also be placed in a gitignored `.env.local` next to `pyproject.toml` (see `.env.local.example`); `server.py` loads it at startup without overriding variables already present in the environment. Useful when starting the server from a context with no shell to `export` into.

## Structure

- `exchange_mcp/` — MCP server package (60 tools)
  - `server.py` — `MCPServer` (mcp SDK v2) with lifespan context; launches the browser on the persistent profile and, if that profile isn't signed in, opens a visible sign-in window (off the handshake path — see "Authentication" below)
  - `browser_session.py` — `BrowserSession`: one persistent Chromium context for the process's lifetime, reused by every OWA call
  - `owa_client.py` — OWA API client; delegates transport to `BrowserSession`, keeps the request/response/folder-resolution logic
  - `auth_errors.py` — Pure diagnosis of a *timed-out* interactive sign-in: reason codes, the AADSTS/page-text/URL hint tables, per-reason remediation text, and `AuthenticationRequiredError`. Imports nothing else from the package (no Playwright) so it stays unit-testable — see "Authentication" below.
  - `availability_frame.py` — Pure logic for *which clock* an availability timestamp is on, and the conversion onto the mailbox's working-hours grid: the Windows→IANA timezone table, `ZoneResolution` (including the "couldn't resolve, so don't shift, and say so" case), and the two parsers that keep the codebase's naive-UTC and wall-clock conventions apart. No Playwright, unit-testable — see "Availability time frames" below.
  - `mailbox_identity.py` — Pure resolution of *our own* mailbox's SMTP address from the signals a session already carries (`x-anchormailbox`, a `GetOwaUserConfiguration` response, Bearer-JWT claims), in that order of authority. No Playwright, unit-testable. `OWAClient.resolve_own_mailbox()` is the cached, one-request-at-most, never-raising wrapper — see "Own mailbox address" below.
  - `profile_lock.py` — Pure diagnosis of a profile directory that another browser already owns: lock-state probes, stale-vs-live judgement, the launch-error hint table, and `ProfileLockedError`. No Playwright, unit-testable — see "Recovery" below.
  - `discovery_session.py` — `DiscoveryRecorder`: a *second*, independent Chromium on a throwaway profile, visible and driven by the user, recording every API call and UI action. Shares nothing with `BrowserSession` but the OWA URL — see "Capability discovery" below.
  - `capability_inventory.py` — What this server already implements, derived by `ast`-scanning `tools/*.py` and `owa_client.py` for transport call sites, plus PROJECT_STATUS.md for the ID-numbering state. No Playwright, unit-testable.
  - `capability_classify.py` — Verdicts and implementation proposals for a recorded capture. Pure logic; the domain knowledge lives in two keyword tables, correctable in one place like `auth_errors.py`'s.
  - `tools/` — Tool modules: email, calendar, categories, people, folders, availability, analytics, auth, copilot, tasks, discovery
- `tests/unit/` — Pure-logic tests, no live mailbox / browser / `EXCHANGE_OWA_URL` needed. Run them all with `python -m tests.unit` (`tests/unit/__main__.py` **discovers** every `test_*.py` and calls its `main()`, so a new suite is picked up with no list to update — that's what CI runs), or one at a time as `python -m tests.unit.<suite>`. Every suite must expose `main() -> bool`; one that doesn't is reported as a failure, not skipped. Separate from `tests/smoke/`, which is live-mailbox end-to-end and can't run unattended.
- `tests/smoke/` — Live-mailbox end-to-end suites, one module per tool group, run individually (see "Running" below). Shared helpers: `server_manager.py` (start/reuse the HTTP server, and say loudly which it did), `mcp_client.py` (`session()`/`call()`), `results.py` (`record()` → `.state/results.jsonl`), and `config.py` — the **one** place the mailbox's own address is resolved, from `EXCHANGE_SMOKE_SELF_EMAIL`. No test module may hardcode a real address: this repository is public.
- `.github/workflows/ci.yml` — CI: `pip install -e .` then `python -m tests.unit`, on every push and PR, across Python 3.10-3.14 plus one Windows job. No secrets, no mailbox, and deliberately **no** `playwright install chromium` — Playwright's Python package is needed to *import* the tool modules, but no unit test launches a browser. `tests/smoke/` is intentionally not in CI.
- `.claude/skills/owa-capability-discovery/` — Interactive skill driving the discovery tools: scope → record → classify → propose → implement. Its `references/implementation-checklist.md` is the "turn a proposal into a tool" procedure.

## Running

```bash
export EXCHANGE_OWA_URL=https://owa.example.com

pip install -e .               # Install MCP server
exchange-mcp-server            # Run MCP server (stdio transport, spawned per client session)
exchange-mcp-server --show-browser  # Same, with a visible browser window
exchange-mcp-server --stable        # Same, excluding known-buggy tools from the MCP tool listing

# Persistent local server instead of per-session stdio spawn (start manually,
# no autostart mechanism — must already be running before a client connects):
exchange-mcp-server --transport http --port 8765

# Persistent local servet to use during smoke test
exchange-mcp-server --transport http --port 8765 --show-browser

# Pure-logic tests (no mailbox, no browser, no EXCHANGE_OWA_URL).
# This is exactly what CI runs; suites are discovered, not listed.
python -m tests.unit

# ...or one suite at a time, while working on it
python -m tests.unit.test_auth_errors
python -m tests.unit.test_availability_frame
python -m tests.unit.test_profile_lock
python -m tests.unit.test_recurrence_expansion
python -m tests.unit.test_capability_classify
python -m tests.unit.test_mailbox_identity
python -m tests.unit.test_item_errors
python -m tests.unit.test_conversation_paging
python -m tests.unit.test_copilot_answer_text
python -m tests.unit.test_recipient_list
python -m tests.unit.test_folder_id_dict
python -m tests.unit.test_folder_resolution
python -m tests.unit.test_meeting_response

# Live-mailbox smoke tests: one module per tool group, run individually.
# The harness starts its own server on 127.0.0.1:8765 if nothing is listening
# there; set EXCHANGE_SMOKE_HOST/EXCHANGE_SMOKE_PORT to reuse a server that is
# already running instead — two servers can't share one browser profile
# directory. Since 2026-09-15 the second one says so (`ProfileLockedError`,
# issue #11); note it is this server that refuses, not Chromium — Playwright's
# Chromium will happily launch a second browser on a profile the first is
# holding, which is the silent corruption risk that check exists to prevent.
python -m tests.smoke.tests.test_copilot
EXCHANGE_SMOKE_PORT=8767 python -m tests.smoke.tests.test_copilot

# Any suite that mails/invites/queries the mailbox itself needs its own SMTP
# address in EXCHANGE_SMOKE_SELF_EMAIL — it is deliberately not in the source
# (public repo; see tests/smoke/config.py). Unset, those suites stop before
# their first tool call with a recorded TOOL_ERROR naming the variable, rather
# than guessing an address and mailing a stranger. Export it once per shell:
export EXCHANGE_SMOKE_SELF_EMAIL=you@example.com
python -m tests.smoke.tests.test_email_lifecycle
EXCHANGE_SMOKE_SELF_EMAIL=you@example.com python -m tests.smoke.tests.test_find_person
# A suite that mails the mailbox itself takes its address from the
# environment instead of a constant (this repo is public):
EXCHANGE_SMOKE_SELF_EMAIL=you@example.com \
    python -m tests.smoke.tests.test_move_email_custom_folder
```

The suites that require `EXCHANGE_SMOKE_SELF_EMAIL` are `test_email_lifecycle`,
`test_email_category_tagging`, `test_calendar_lifecycle`, `test_calendar_category_tagging`,
`test_folder_lifecycle`, `test_move_email_nested_folder`, `test_find_person`,
`test_find_meeting_time` and `test_get_meeting_stats`. `test_email_flag` prefers it too but
can fall back to reading the address off Sent Items. The lookup lives in exactly one place —
`tests/smoke/config.py` (`require_self_email()` / `discover_self_email()`) — so a new suite
that needs the address imports it from there rather than adding another `os.environ.get`,
and **never** reintroduces a literal address: that is how one got committed to a public
repository in the first place.

**Never start the server with `python -m exchange_mcp.server`** — it registers *zero* tools and
every call fails `Unknown tool`. `-m` loads `server.py` under the name `__main__`, so when each
tool module does `from exchange_mcp.server import mcp` Python imports the module a *second* time
and builds a *second* `MCPServer` instance: the `@mcp.tool()` decorators land on one, `main()`
serves the other. Use the `exchange-mcp-server` console script. The one case where that isn't
enough is running a **worktree's** code: `pip install -e .` resolves to its original path
regardless of cwd, so the console script runs the main checkout no matter where you invoke it.
For that, `cd` into the worktree and use
`python -c "from exchange_mcp.server import main; main()" --transport http --port <port>` —
`python -c` puts cwd first on `sys.path`, and importing `exchange_mcp.server` by its real name
keeps the single `MCPServer` instance. Verify with a `list_tools` count of 60 before trusting a run.

There is no credential setup step and no login CLI: the first start opens a browser
window and you sign in there. See "Authentication" below.

Dependencies: `mcp` (v2 — see below), `playwright` (run `playwright install chromium` once), `tzdata`. That last one is not padding: **Windows ships no IANA timezone database**, so on a stock Windows install `zoneinfo.available_timezones()` is empty and every zone lookup raises `ZoneInfoNotFoundError` — which is the platform this repo is developed on, and slim Linux containers have the same hole. `availability_frame` needs a real zone to put UTC busy periods on the mailbox's working-hours grid; without the package it degrades to "not shifting, with a warning in the tool's own output" rather than failing, so a missing dependency shows up as a caveat rather than an error. An environment installed before it was added needs `pip install -e .` again. `mcp`'s `streamable-http` transport (`uvicorn`/`starlette`) is already a transitive dependency — no extra install needed for `--transport http`. An environment that predates the v2 migration is on `mcp` 1.x, which this code cannot import: re-run `pip install -e .` (1.x no longer satisfies the requirement, so pip upgrades it) and restart any long-running server.

**`mcp` requires `>=2.2.0,<3`** in `pyproject.toml`: this package is on the **v2 SDK** (migrated 2026-09-14; it was pinned `<2` for a few days after a clean `pip install -e .` in CI resolved 2.x and the package stopped importing at all, issue #16). What that means when reading or writing code here:

- The server object is `MCPServer` (`from mcp.server.mcpserver import MCPServer`), and tool modules import `Context` from the same place. `mcp.server.fastmcp` is gone — in 2.2.0 it exists only to raise a `ModuleNotFoundError` that names the migration guide.
- **Transport settings are `run()` arguments, not constructor settings.** `mcp.settings.host = ...` now *raises* (`Settings` keeps only constructor-owned fields), so host/port go to `mcp.run(transport="streamable-http", host=..., port=...)`. Passing a loopback `host` there is also what arms the SDK's Host/Origin validation.
- **The lifespan is entered once per server process, on both transports.** Under 1.x the streamable-http session manager re-entered it per *client session*; 2.x enters it at ASGI startup and shares the state. `_ensure_started()` is idempotent, so nothing here depended on the old behaviour, but comments that explained it did (see `AppContext`).
- **Wire model fields are snake_case**: `isError` → `is_error`, `structuredContent` → `structured_content`, `serverInfo` → `server_info`, `inputSchema` → `input_schema`. Client-side, `streamablehttp_client` is `streamable_http_client` and yields two streams, not three — the `get_session_id` callback is gone (`tests/smoke/tests/test_mcp_session_lifecycle.py` shows the supported way to recover the id).
- **`serverInfo.version` is reported verbatim and defaults to `""`**, where 1.x silently substituted the SDK's own version, so `server.py` passes `version=__version__` explicitly. That is the number the smoke harness's identity probe prints.
- **2.2.0 reaps idle streamable-http sessions after 30 minutes** (1.x never did), and a reaped id answers exactly the 404 `"Session not found"` that issue #18 was filed about — so `server.py` passes `session_idle_timeout=None`. Relatedly, a *cleanly terminated* id now gives that same text instead of `"Not Found: Session has been terminated"`, because 2.x discards a session however it ended; the wording no longer tells the two apart.
- v2 does not depend on `pydantic-settings`, and `Settings` is a plain `BaseModel`, so the `IncompleteFieldDefinitionWarning` filter 1.x needed at the top of `server.py` is gone. Don't reintroduce it: the import itself would fail on a clean install.

## Architecture

**Browser-per-call transport**: Every OWA call — not just login — goes through one persistent, real Chromium instance instead of a plain HTTP client, because OWA now requires signals (a fresh per-page CSRF canary, browser-like headers, a real TLS/JS fingerprint) that a hand-rolled `requests` session replaying exported cookies can't replicate. `BrowserSession` launches Chromium once via `launch_persistent_context` (on-disk profile, survives restarts and benefits from Microsoft's "stay signed in" cookie). Each tool call opens its own tab against that same context, performs its fetch, and closes the tab. A background thread with a dedicated asyncio loop hosts Playwright's async API; `OWAClient`/tool code call into it through plain synchronous methods.

**Authentication — profile-first, interactive, no credentials**: the persistent browser profile *is* the session. There is no credential store, no master password, and no login CLI: the server never types a password anywhere. Startup (`_startup` in `server.py`, on a background thread started by `_ensure_started()`) does exactly this:

1. Resolve the profile directory (`EXCHANGE_BROWSER_PROFILE_DIR`, else `default_profile_dir()`), reuse it if it exists, create it if not — `BrowserSession.profile_existed` is captured before anything creates it so the log can tell the two apart honestly.
2. `BrowserSession.has_active_session()` — if the profile is still signed in (live OWA cookies, Microsoft's "stay signed in" cookie, or an SSO session the modern SPA can still mint a Bearer token from), serve immediately.
3. Otherwise `BrowserSession.interactive_login()`: relaunch the context **visible** (`_async_relaunch(headless=False)`), park it on the OWA sign-in page, and poll until a session appears or `EXCHANGE_LOGIN_TIMEOUT` expires. The *user* signs in — address, password, 2FA — we only watch for the result.
4. If nobody completes it, keep serving anyway. Tools report `authorization_required` and the `login` tool reopens the window on demand. A stdio server is routinely spawned while the user is away from the keyboard, so dying for that reason would be worse than waiting to be asked.

Deliberate design points, each of which has a wrong-looking-but-tempting alternative:
- **The interactive login never bails out early on a recognized error.** A human is sitting in front of that window: a mistyped password, an accidentally denied MFA push, or a redirect to a change-password page are all things they can simply carry on from. `auth_errors.classify_login_failure()` therefore runs *once, on timeout*, purely to explain what the page was showing when we gave up — it is diagnosis, not control flow.
- **After a successful interactive login the window stays visible** for the rest of the process's life (`interactive_login` says so in its message). Relaunching back to headless would tear down the context seconds after the session landed in it; a window on screen is a far cheaper failure mode than a login that doesn't stick. Restart the server to get back to headless.
- **`ensure_logged_in()` is silent-only and never opens a window.** It's what `OWAClient` calls on a 401, and a Chromium window appearing in the middle of an unrelated tool call would be hostile. When it fails, `OWAClient._relogin_or_raise()` raises `AuthenticationRequiredError` (not `SessionExpiredError` — that one means "retry after re-login", this one means "a human must sign in"), whose message points at the `login` tool. `check_session` and `login` surface it structurally as `"authorization_required": true` plus `reason` and `remediation`.
- **`login` is a two-call tool** (`login()` → window opens, returns immediately → user signs in → `login()` again reports the result), because an interactive sign-in takes minutes and an MCP request can't be held open that long. `force=True` opens the window even when the session looks fine, for switching accounts.
- **Startup is kicked off from `main()`, not just from `app_lifespan`.** Under `--transport http` the MCP lifespan runs *per client session*, so nothing at all happened until something connected — no profile directory, no browser, no sign-in window on a freshly started server. `main()` calls `_ensure_started()` before `mcp.run()`; `app_lifespan` still calls it too (idempotent) so an embedder that serves `mcp` directly gets the same setup. `_startup` runs on a `threading.Thread`, not an asyncio task, both because there is no event loop yet under http and because every `BrowserSession` method is already synchronous. `_shared_state_lock` is a `threading.Lock` for the same reason: an `asyncio.Lock` binds to the first loop that touches it and rejects every other one.
- **Startup logging is the contract with the operator**, and every line goes to stderr via `_log()` (stdout is the stdio transport's JSON-RPC stream). The banner reports the version, OWA URL, resolved profile directory *plus why it resolved that way* (`_profile_dir_source()`) *plus whether it already existed*, and headless vs. visible; then `Auth status: AUTHENTICATED / NOT AUTHENTICATED / UNKNOWN` with the reason and remediation. The "why" matters: the source-checkout vs. installed-package profile split is otherwise invisible, and `pip install -e .` counts as a checkout — someone expecting `~/owa-mcp/` gets the repo path and no explanation.
- Page scraping (`_async_detect_login_failure`) reads `AADSTS<code>` tokens from the whole HTML (precise enough to be safe there) but free text only from *visible* error containers (`VISIBLE_ERROR_SELECTORS`) — the Entra ID page ships hidden templates whose wording ("update your password", ...) is present on a perfectly healthy page. Add new signals to the tables in `auth_errors.py`, not to the scraping code, and cover them in `tests/unit/test_auth_errors.py`.

**OWA JSON API pattern**:
1. Open a new browser tab, read the current `X-OWA-CANARY` cookie from the persistent context
2. `fetch()` the request from inside that tab (`page.evaluate`), capturing the real response via `page.expect_response` for status/headers/body
3. POST JSON to `$EXCHANGE_OWA_URL/owa/service.svc?action=<ACTION>`
4. Request bodies use EWS `__type` annotations (e.g. `"CalendarItem:#Exchange"`)
5. HTTP 401/440 = session expired → `OWAClient._relogin_or_raise()` calls `BrowserSession.ensure_logged_in()` (silent re-auth against the profile only) and retries once, or raises `AuthenticationRequiredError` if the profile can't carry us either
6. **Ask for the fields you need, not `AllProperties`.** A read shape is not free: on this backend `BaseShape: "AllProperties"` makes OWA's own serialiser throw `SerializationException` (HTTP 500) on `MeetingRequestMessage` items, *mid-response* — the 500 body is truncated valid JSON, so no request-side change fixes that shape. The same items read fine at `IdOnly` + named `PropertyUri` entries (`_get_item_categories` in email.py, `get_email_links`). Treat an `AllProperties` 500 as "this shape is too wide", not "this item is unreachable" — writes to such items work normally. Note the opposite trap in tasks.py: there a single bad `FieldURI` in `AdditionalProperties` fails the whole request, so narrow shapes want *known-good* spellings, not guessed ones.

**Per-item error codes**: tools taking a list of `item_ids` never abort the batch on one bad item, and report each failure with a stable `error_code` from `utils.classify_item_error()` (`item_not_serializable` / `item_not_found` / `item_access_denied` / `item_read_failed`), lifted to `failed_codes` on the batch summary. That exists because client skills were reduced to substring-matching an HTTP 500 message to decide whether to fall back to another connector. Add new signals to the tables in `utils.py`, not to the tool modules, and cover them in `tests/unit/test_item_errors.py` — an unrecognised failure must stay `item_read_failed` rather than be guessed into a specific code.

7. **Page with the server's `Offset`, and only stop on an *empty* page.** A caller-supplied `offset` must go into `Paging.Offset` (`IndexedPageView`), never be applied by slicing one response — `get_emails` did the latter and turned every offset past its own window into an indistinguishable `{"count": 0}` (see PROJECT_STATUS.md §4, fixed 2026-09-11). The corollary matters just as much: because this backend doesn't reliably respect `MaxEntriesReturned`, a **short page does not mean end-of-folder** — only a genuinely empty one does, so a paging loop pays one extra request at the end rather than risking silent truncation. Tools that page must also make an empty result say *why* it's empty (`_page_conversations`'s `pagination` block: `has_more`/`next_offset`/`reached_end_of_folder`, plus an `error_code` when paging stopped early). Keep that diagnosis nested, not at the top level — a top-level `error` key means the whole call failed. A tool whose filter is client-side (`get_emails`' `unread_only`, `find_emails_by_category`'s category match) must reuse `email.py`'s `_scan_conversations`/`_conversation_page` rather than write its own loop: the empty-page-only stop rule and the ignored-`Offset` witness are precisely the parts a second implementation drops, which is how `find_emails_by_category` shipped the same silent truncation one function away from the fix (§4, 2026-09-14).

8. **Resolve a folder through `OWAClient.resolve_folder()`, and test for an ID before anything else.** It is the single gate in front of every folder-taking argument (`move_email`'s `target_folder`, `get_emails`/`search_emails`/`find_emails_by_category`'s `folder`, the tasks reads), and the ordering *is* the contract: opaque ID → `/`-path → distinguished name → top-level display name → Inbox child → unique match anywhere (Deep) → bare distinguished ID. Two tiers exist because of live failures, not tidiness. The ID check has to come first because EWS folder IDs are base64 blobs that routinely contain `/`, so a resolver that splits on `/` first destroys the one form a caller cannot get wrong (`looks_like_folder_id()`; that bug made "pass the exact ID `get_folders` returned" fail identically to a typo). The Inbox-child tier exists because the reported-missing folder was a child of the Inbox all along — a *recursive* `get_folders` on `msgfolderroot` flattens the tree, so nesting is invisible in its output and a caller reasonably reads a name there as top-level. And a name matching several folders must return `folder_name_ambiguous` with every candidate ID rather than the server's first hit: mailboxes here have many near-identical short names, and filing mail into the wrong one silently is worse than failing. Folder *listing* follows the same empty-page-only paging rule as item paging above (`iter_child_folders()`) — a single 200-entry `FindFolder` silently truncated large mailboxes, which made every folder past the first page unresolvable. Cover changes in `tests/unit/test_folder_resolution.py`, whose fake transport asserts request *counts* too (an ID must resolve with zero requests).

**RequestServerVersion**: `Exchange2013` for reads, `V2017_08_18` for writes.

**Own mailbox address — always through `OWAClient.resolve_own_mailbox()` / `mailbox_address()`, never a stored field.** There is no signed-in username to remember: authentication is a browser profile, so *who we are* has to be discovered from the session. `resolve_own_mailbox()` does it once per process from three signals in descending order of authority — `x-anchormailbox` (captured with the bearer token, no request), `GetOwaUserConfiguration` (one request), then Bearer-JWT claims — and caches the result, including a failure (the realistic failure is a backend with no such surface, and re-probing would add a request to every availability call forever). `login(force=True)` is the only event that can change the answer, so it calls `forget_mailbox_address()`. The parsing lives in `mailbox_identity.py`; add signals to its tables and cover them in `tests/unit/test_mailbox_identity.py`, not in `owa_client.py`. Three rules are load-bearing, all of them paid for by the six days (2026-09-10 to 2026-09-16) when the predecessor field `user_email` had five readers and no writer (PROJECT_STATUS.md §4):
- **It never raises**, because the callers are tools with something useful to do without it (`find_free_time` scans the calendar folder instead; `get_schedule` can send an attendee's id as the requesting user). An identity probe must not be able to take a mailbox tool offline.
- **It never guesses.** A real `x-anchormailbox` also carries `PUID:<hex>@<tenant guid>` — an opaque id containing `@` — and `get_meeting_contacts` excludes "self" by comparing against this value, so a *wrong* address silently corrupts a result while an empty one is reported. Anything not positively recognisable as an SMTP address resolves to `""` plus a `reason`.
- **A tool that falls back must say so.** `find_free_time`'s fallback (`FindItem` over the calendar folder) does not expand recurring masters, so it reports booked time as free; it therefore returns `busy_source` and a `warnings` entry rather than an answer that merely looks thinner. That silent fallback is the bug this rule exists for.

**Recovery**: if the browser process/context crashes, `BrowserSession` relaunches on the same profile directory and retries the call once. If the OWA session expires, `OWAClient` retries once after a silent re-auth attempt — see "Authentication" above for what happens when that can't succeed.

**A profile another browser already owns is refused, not retried (`profile_lock.py`, issue #11)**. One profile directory has exactly one owner, and the failure mode that gap produced was pathological: an orphaned Chromium tree holding `.browser-profile` made every launch — *including the crash recovery's own one-retry* — report only `Target page, context or browser has been closed`, which named neither the profile nor the real cause. So `_async_ensure_context` now inspects the directory *before* launching, waits out a lock that is merely transient, and raises `ProfileLockedError` (naming the directory, the owner PID where knowable, and the remediation) if it is genuinely held; `_run_with_recovery` re-raises that ahead of its `_CRASH_HINTS` check, because relaunching into a live lock is the exact non-recovery this issue is about. Four things are load-bearing:

- **The error text alone cannot decide it**, which is the structural difference from `auth_errors.py`: a crashed browser and a contended profile produce the *same* closed-context message. `classify_launch_failure()` therefore takes the launch error *plus* a live look at the directory, and returns `None` (i.e. "keep your generic crash handling") for a bare "has been closed" — guessing `profile_locked` from that would relabel every ordinary crash as an operator error.
- **Detection has to happen before the launch, because the launch does not fail.** Verified 2026-09-15: Playwright's Chromium does **not** refuse a second `launch_persistent_context` on a profile a live browser holds — it launches, is usable, and quietly shares the `user-data-dir` that Chromium's own ProcessSingleton exists to keep single. So "two servers can't share one profile" is a hazard here, not an enforced error, and waiting for a lock error would wait forever.
- **`SingletonLock` is POSIX-only.** Windows Chromium uses a named mutex and writes no such file, so the documented `<host>-<pid>` symlink — the only artifact that names an owner, and therefore the only one that can be judged *stale* — is unavailable there. The cross-platform signal is the OS lock Chromium holds on its LevelDB `LOCK` files (`_LOCK_PROBE_FILES`); note that only *some* of them are held on a live profile (measured: three held, `PersistentOriginTrials/LOCK` free), so any one held file is decisive while "free" needs every present file to agree.
- **Only a positively-detected live lock blocks a launch, and only a positively-dead owner is cleared.** Every undecidable case returns `unknown`, which never blocks: a fabricated lock takes a healthy server offline, which is worse than the confusing message being replaced. `clear_stale_lock()` unlinks Chromium's singleton artifacts *only* when an owner was identified and found gone, and nothing in that module ever touches a process — the orphaned-tree case still needs a human with a task manager, the change is that they are now told so. Add new signals to the tables in `profile_lock.py`, not to `browser_session.py`, and cover them in `tests/unit/test_profile_lock.py`.

**Transport (`stdio` vs `http`)**: `main()` picks the transport via `--transport`/`EXCHANGE_MCP_TRANSPORT`. The lifespan that creates the `BrowserSession` runs exactly once per process either way (on v2 that is the SDK's own behaviour; 1.x re-entered it per streamable-http client session, which `_ensure_started()`'s process-wide state already absorbed) — under `stdio` that process is spawned and killed per client session, so the warm browser/login is rebuilt every time; under `--transport http` the process is long-lived and the same `BrowserSession`/login is shared across every client connection that hits it, but it must be started manually — there is no autostart mechanism. Never bind `--host`/`EXCHANGE_MCP_HOST` off `127.0.0.1` — the MCP endpoint has no auth of its own, and the SDK's `transport_security` (Host header validation) must stay enabled to block DNS-rebinding from other pages in the user's browser. Under v2 that protection is armed by the `host` passed to `run()` — the SDK auto-enables it for `127.0.0.1`/`localhost`/`::1` — so moving the bind address off loopback silently disarms it as well as exposing the port.

**Task tools (`tools/tasks.py`)**: `Task` is Exchange's own name for the item class
(`Task:#Exchange`, `IPM.Task`) in the `tasks` distinguished folder, and the modern web UI
surfaces the same items as **Microsoft To Do** (`outlook.cloud.microsoft/host/<app-guid>/ToDoId`),
where each To Do *list* is a child folder of that root. So these tools use the ordinary EWS item
actions (`FindItem`/`GetItem`/`CreateItem`/`UpdateItem`/`DeleteItem`) rather than To Do's own
private REST surface — that path works on both backends and needs no new transport. Three
non-obvious constraints, all documented at length in the module docstring: reads use
`BaseShape: "AllProperties"` with **no** `AdditionalProperties` (one bad `FieldURI` fails the whole
request on this backend), every write-side `FieldURI` spelling lives in the module's `_FIELD` dict
so a live-test correction is one line, and task `DueDate`/`StartDate` are written as UTC midnight
(`…T00:00:00.000Z`) because that's how Exchange stores them — a local-midnight write comes back a
day off. `Status`, `PercentComplete` and `CompleteDate` are three spellings of the same state and
the last one Exchange processes wins, so never send two in one request. There is no task-list
(folder) CRUD here: a To Do list is a plain folder, so `get_folders(parent_folder_id="tasks")`
and the `*_folder` tools cover it, *including* creating one — `create_folder(name=…,
parent_folder_id="tasks", folder_class="IPF.Task")`. Note what that argument has to defend
against: EWS reads a `FolderClass` as a **prefix** and silently treats any unrecognised one as
`IPF.Note` instead of erroring, and the class is immutable afterwards, so a typo produces a
working *mail* folder whose only symptom is `get_tasks` finding nothing in a folder that
plainly exists. `create_folder` therefore returns the class read back from the server's own
`CreateFolder` response rather than echoing the argument.

**Calendar category tools (`tools/calendar.py`)**: a full calendar tagging pass runs entirely over OWA — `get_calendar_events(..., include_body=False)` to list, then `assign_event_categories` / `remove_event_categories` to write. No Outlook COM fallback is needed and none should be reintroduced (re-verified live 2026-09-14; PROJECT_STATUS.md rows 201/208/209). Two things worth knowing before assuming otherwise, because both look like the opposite: **`categories` is a list-mode field**, populated from the single `CalendarView` request rather than a per-item `GetItem` — unlike email's `flag_status`, which really does need `include_body=True` to be trustworthy — and the *write* path is not EWS `UpdateItem` at all but the bespoke `UpdateCalendarEvent` action captured from OWA's own client (`_set_event_categories`; plain `UpdateItem` fails with `ErrorSendMeetingInvitationsOrCancellationsRequired` whatever notification attribute you send with it, see #208/#209). Attendees are never notified, and tagging a `RecurringMaster`'s `item_id` tags the whole series — which is why synthesized occurrences from `expand_recurrences` carry an empty `item_id`.

**Availability time frames (`availability_frame.py`, `tools/availability.py`, `tools/analytics.py`)**: the availability tools work on exactly one clock — the mailbox's own wall clock — because everything user-facing about them is local: `start_hour`/`end_hour`, the day keys the results are grouped by, the weekend skip, and the `HH:MM` strings in the output. What made that a bug rather than a convention is that **the two free/busy surfaces in the same response disagree about their frame**, and only one of them needs converting:

- `availabilityView` / `MergedFreeBusy` (the `0/1/2/3/4`-per-interval string) is wall-clock in the *requested* `tz_id`, so `_parse_freebusy_string` laying it out from the caller's own `start_date` at local midnight is already right. **Do not convert it** — instead *ask* for it in the zone you will compare it against (`frame.schedule_tz_id()` → `get_schedule(tz_id=...)`). That is the only lever this path has, and forgetting it is not a subtle failure: measured live on 2026-09-16, converting the events while still requesting the window in the old hardcoded UTC+3 made `find_free_time` and `find_meeting_time` answer grids an hour apart for the same mailbox on the same day.
- `scheduleItems` — one field over in the same GraphQL response — arrives UTC-offset whatever `tz_id` is requested, and `_parse_schedule_dt` returns it as naive **UTC**. Comparing that against the local working-hours window slid every meeting by the mailbox's UTC offset (2h for a CEST mailbox in summer), so `find_free_time`/`find_meeting_time` offered booked time and hid free time with no error anywhere. Fixed 2026-09-16; PROJECT_STATUS.md §1/§4.

So a change here has to say *which* path it is touching, and a fix that converts "the availability timestamps" as one category breaks the working one in the opposite direction. Four things are load-bearing:

- **The conversion is driven by the wire, not by an assumption.** `wire_to_wall_clock()` converts a timestamp only if it actually carried an offset, which is what lets one helper serve both `scheduleItems`/`FindItem` (UTC-suffixed) and EWS `CalendarEventArray` (unsuffixed, already in the `TimeZoneContext` zone) without either path depending on a guess about the other. `to_utc_naive()` is the other convention, kept deliberately distinct and named so it can't be reached for by accident.
- **`OWAClient` keeps returning naive UTC, and the tools convert.** `_parse_schedule_dt`'s return type is assumed by both tool modules, so widening it would be the larger and less reviewable change; the conversion belongs next to the grid it has to match. Note the one fix made *inside* that convention: an offset other than `Z`/`+00:00` used to have its `tzinfo` stripped outright, which keeps that zone's wall clock and labels it UTC.
- **An unresolvable zone shifts nothing and reports it** (`ZoneResolution.warning`, surfaced as `"timezone": {...}` on #601/#602 and through the existing `warnings` field on #701/#702). A fabricated offset is worse than the known-imprecise status quo, because an answer that is wrong by one hour looks right — the same reasoning as `profile_lock.py`'s "only a positively-detected lock blocks a launch".
- **Timezone *ids* need translating and the table is the place for it.** Exchange speaks Windows ids (`W. Europe Standard Time`), `zoneinfo` speaks IANA (`Europe/Berlin`), and the modern backend has been seen handing IANA ids to its own web client — so both are accepted, `_WINDOWS_TO_IANA` maps the former, and an unmapped id is unresolvable-with-a-warning rather than a guess. Add spellings to that table, not to the resolver, and cover them in `tests/unit/test_availability_frame.py` (whose injectable `loader` is how both failure branches are tested on a machine with no tz database).

The zone itself comes from `OWAClient.mailbox_timezone()` once issue #8 lands, and from `EXCHANGE_TIMEZONE` until then (`timezone_id_from_client`, resolved via `getattr` so the module works on either side of that change) — a bridge to delete, not a second copy of the precedence rule.

**Copilot tools (`tools/copilot.py`)**: unlike every other tool module, Copilot has no documented API to call — there is no EWS action, no REST endpoint, nothing to POST. These tools instead drive Copilot's own chat pane inside the modern Outlook web client directly via Playwright UI automation (`BrowserSession`'s Copilot section: `_async_copilot_locate_pane`/`_open_pane`/`_submit`/`_wait_and_read`/`_async_copilot_ask`/`copilot_ask()`), the same "automate OWA's own web UI" escape hatch already used for the calendar category write-path (`_set_event_categories`, see PROJECT_STATUS.md #208/#209) when no API exists. Only available in `bearer` auth mode (modern Outlook) — raises `BearerModeRequiredError` on classic canary-cookie OWA, the same exception `find_people`/`post_substrate` use for their own modern-backend-only surfaces. **The one thing to know before touching this module: the chat pane is a cross-origin iframe.** OWA runs on the mailbox host; the pane is served from `m365copilotapp.svc.cloud.microsoft` (`_COPILOT_FRAME_HOST_HINTS`). A Playwright `page.locator(...)` only ever searches the main frame, so *any* selector written against the page — however well guessed — cannot match a node in the pane. That is what made all five tools fail their first live run, and it's why `_async_copilot_frame` resolves the frame before anything else and every other helper takes a locator already rooted inside it. The launch button is the one exception: it's genuine main-frame OWA chrome, matched on the accessible name "Copilot" (which Microsoft doesn't translate — the prompts inside the pane *are* localised, hence `_COPILOT_STOP_HINTS`).

Discovery capture `20260911-112708-e917` also established that **no HTTP endpoint carries the prompt or the generated answer**, so there is no transport to migrate to and the UI automation is not a temporary shim. Whether generation rides a WebSocket is still open — the recorder was blind to WebSockets when that capture ran and now isn't. Two shapes it *did* confirm, both already correct in the code: the mail deep link `/mail/<folder>/id/<urlencoded id>` and the event deep link `/calendar/item/<urlencoded id>`. A calendar *item* page has no Copilot launcher, though, so `copilot_ask` takes a `launcher_fallback_url` (the calendar view) for events.

**All five tools pass `tests/smoke/tests/test_copilot.py` as of 2026-09-11** (rows #901-905 are `OK`/`Stable`, and `KNOWN_BUGGY_TOOLS` is now empty). Getting there needed three more fixes beyond the iframe one, each of which the capture could not have predicted — and two of them fail in ways that *look like* success, which is why they matter more than the selector work:

- **The iframe is created and then replaced** during Copilot's own load, so resolving it once returns a corpse. `_async_copilot_open_pane` polls until the frame is *usable* (live, with a composer in it) rather than merely present, and `_async_copilot_frame` skips detached frames — a detached frame lingers in `page.frames` with a matching URL, so the grounding `page.goto()` left every second call reading the previous call's dead pane. It surfaced as `Locator.wait_for: Frame was detached` on one tool and "no textbox to type into" on another; one race, two unrelated-looking bugs.
- **`pane.inner_text()` is the whole panel, not the answer**, and the pane's pre-answer chrome is already non-empty *and* already stable — so the completion heuristic returned Copilot's UI as `{"status": "ok"}` about three seconds in, before Copilot had answered at all. `_async_copilot_wait_and_read` therefore takes a `baseline` (the pane's text from before submitting) and will not call anything settled until the text has changed from it; if nothing ever changes it returns `status: "no_response"` with a content-free `pane_structure`, never a fake partial. `_copilot_answer_text` then isolates the reply: everything after the last `Copilot said:` marker, minus baseline lines (which is what removes the composer's own placeholder), minus the pane's button labels (which is what removes the follow-up suggestion chips — their wording is generated per answer, so no hint table can cover them, but "it is a button" always holds).
- **Text is not a completion signal at all; `aria-busy` is** (2026-09-15). Two text-based rules shipped and both failed the same way — the pane renders an in-flight status line (`"In corso…"` on it-IT) and, worse, plateaus mid-answer, and a progress state is by construction *new relative to `baseline`* and *unchanging*, so it satisfies every text defence. A 444-sample DOM probe of one live generation settled it: `aria-busy="true"` covered the generating window exactly (t=0.0-48.0s, final text at t=49.4s), while the text sat at 377 chars for 27 consecutive polls with the real 8836-char answer still 46 seconds away. So `_async_copilot_wait_and_read` **latches** `saw_busy`: once the DOM says generation started, its clearing is what "finished" means, and text stability is only the ~1.4s catch-up afterwards. Two traps worth knowing: `data-testid="loading-message"` is the obvious-looking candidate and is *useless* (visible in 444/444 samples — a permanent element, not a flag), and the marker check must count turns against the baseline, not test presence, because **the pane is reused across calls with its transcript intact** so a previous answer's marker is already there. The text heuristics survive only as a fallback for a pane with no `aria-busy`, so a redesign degrades to the old behaviour instead of timing out. `_COPILOT_PROGRESS_HINTS` still matches whole lines, never substrings — the summary the first bug hid contained "In corso" as a table status cell.
- **Acquiring the pane and submitting the prompt retry as one unit**, because the frame is replaced *between* `fill()` and `press()` — so `_async_copilot_submit` hit `Locator.press: Frame was detached` on ~1 call in 5, with the failing tool rotating between runs. Checking harder beforehand cannot fix that (`_async_copilot_open_pane` already polls until the frame is usable; the check is made once and then trusted for the rest of the submit). `_async_copilot_open_and_submit` therefore redoes acquire + baseline + submit together: a dead frame invalidates the pane locator *and* the baseline, and carrying either across a retry means diffing an answer against a stale snapshot. `_copilot_is_frame_swap` keeps the retry narrow — a missing launcher, a classic-OWA tenant and a rate-limit banner all still fail fast. **The guard that is easy to remove by accident**: if the lost `Enter` actually landed, retrying asks Copilot the same question twice, so the turn count is compared against the transcript we typed into, and in that case the *pre-submit* baseline is kept (the fresh one already holds the answer, and using it would subtract the answer as furniture). Verified by 3 consecutive smoke runs, 15/15 calls.
- **The deep links below are confirmed for `outlook.office365.com` only.** This tenant now serves the SPA from `outlook.cloud.microsoft` after a fresh sign-in, and `bearer_origin` follows whatever host minted the token, so those shapes are being applied to a host they were never verified against. Grounding failure is silent by design (falls through to an ungrounded ask), so this degrades answers rather than erroring — see PROJECT_STATUS.md §4.
- **Opening an item does not ground the prompt.** The chat pane is a standalone conversation that does not inherit what is on screen: asked to summarise an open Inbox thread, Copilot answered "non vedo alcun thread email allegato o identificato nel tuo messaggio" and ran a generic mailbox search. `_ask` in `copilot.py` now pastes the item's subject and body *into the prompt* (narrow `IdOnly` + `Subject`/`Body` shape, never `AllProperties`, so a meeting invite doesn't break grounding) and reports `grounded` / `grounding_warning`. Note which tool worked first: `coach_draft`, the one that already put its content in the prompt.

Two of the capture's open questions are now settled: the pane **does** have a free-text composer, and **no** Coaching affordance is needed (#904 is just a prompt). Still open: whether generation rides a WebSocket — the recorder can see them now but no capture has exercised it.

**Capability discovery (`tools/discovery.py`, module 11)**: the only tool module that doesn't
touch a mailbox. It exists because every gap closed in this codebase so far was found the same
way — someone spotted a feature in OWA's own UI, ran `--show-browser`, watched the network tab,
and reverse-engineered the call (the category write-path #208/#209, `GetSchedule` #602,
Substrate search #401). These tools make that loop repeatable: `start_discovery_session`
records a live user-driven session, `classify_discovery_session` sorts every captured endpoint
into *unknown API* / *known API with parameters we never send* / *already covered* and proposes
tools, modules and permanent IDs. The `owa-capability-discovery` skill drives it end to end.

Four things about it are load-bearing:

- **The recorder never touches `BrowserSession`.** It launches its own Chromium on a
  `tempfile.mkdtemp()` profile, always visible. Driving the shared session interactively would
  navigate the anchor page out from under in-flight tool calls, and recording on it would fill
  the capture with this server's own traffic — exactly the traffic that's supposed to read as
  already-implemented. Same loop-on-a-background-thread pattern, different instance.
- **The "already implemented" baseline is derived, not written down.** `capability_inventory.py`
  `ast`-scans the transport call sites, resolves module-level action constants (`_ACTION` in
  categories.py), and credits module-level wire tables (`_FIELD` in tasks.py) to every action in
  that file. That last rule isn't optional: without it `UpdateItem` has *zero* known FieldURIs,
  so every one OWA sends gets reported as a discovery. Both attribution rules over-credit on
  purpose — an over-credited baseline under-reports, and a missed finding costs one more capture
  while a fabricated one costs an afternoon.
- **Redaction is a correctness requirement.** A throwaway profile means every capture runs
  through a real sign-in. Requests to sign-in hosts are dropped before anything is written, only
  an allowlisted set of headers is recorded (never `Authorization` / `Cookie` / `X-OWA-CANARY`),
  the injected page script records tag/role/label but never input values, and response bodies are
  stored as a content-free field/type skeleton unless raw bodies are explicitly requested.
  WebSocket frames follow the same opt-in: by default only the socket's URL, each frame's
  direction and its size reach disk, because a frame on a chat socket *is* mailbox content (the
  prompt, the generated answer). `tests/unit/test_capability_classify.py` asserts each of those
  directly rather than trusting them.
- **HTTP-only would have been a misleading capture.** The recorder hooks `page.on("websocket")`
  as well as `response`/`requestfailed`, because the first real capture
  (`20260911-112708-e917`, the Copilot spike) produced answers on screen with no HTTP request
  carrying them — and a recorder that only sees HTTP reports that as "no endpoint" rather than
  "no endpoint *of the kind I can see*". Those are very different findings to act on.
- **Two filters keep instrumentation out of the findings**, and both were added because that
  same capture was drowned by it: `_NOISE_PATH_HINTS` (here) drops telemetry and client-config
  paths at record time — `/pacman/`, `/clientevents`, `/config/v1/`, the `events.data.microsoft.com`
  hosts — and `_STRUCTURAL_REQUEST_KEYS` (in `capability_classify.py`) drops content-free
  request keys before a verdict. Before them, the loudest "discovery" in a Copilot session was a
  client-event logger with 52 calls, and `GetItem` was reported as having 19 unsent parameters
  of which 18 were HTML-sanitisation options. The structural-key table draws a fine line worth
  respecting: a key that says a *feature is in use* (`Restriction`, `SortOrder`) stays
  reportable; only the grammar inside it (`Path`, `FieldURI`, `Value`) is dropped.
- **Classification reads the capture files, not the live recorder**, so a session survives a
  restart, can be re-classified under a different scope, and can be re-run after implementing a
  proposal as a free check that the endpoint moved to `known_api_covered`.

There is deliberately **no Playwright trace**: `tracing.stop()` must run while the context is
alive, and the normal end of a session is the user closing the last window — a trace could only
ever be written on the abnormal path. Temp profiles are cleaned up with a retry loop (Chromium
releases the profile lock after `context.close()` returns) plus a sweep of stale
`owa-discovery-*` directories at the start of each session, because cleanup runs on a daemon
thread that process exit kills outright.

## Maintaining PROJECT_STATUS.md

[PROJECT_STATUS.md](PROJECT_STATUS.md) tracks, per MCP tool: a permanent ID, automated-test coverage, and manual QA result (`Pending`/`OK`/`KO`). Keep it in sync as part of the same change, not as a follow-up:

- **ID column and numbering rule**: every tool row's first column is a permanent ID of the form `<module number><2-digit sequence within that module>` — 3 digits for the single-digit modules, 4 from module 10 onward (`e.g. 208` = module 2 (Calendar), 8th tool assigned in that module; `1003` = module 10 (Tasks), 3rd tool there). Module numbers are fixed: 1 Email, 2 Calendar, 3 Categories, 4 Directory (`people.py`), 5 Folders, 6 Availability, 7 Analytics, 8 Auth, 9 Copilot, 10 Tasks, 11 Discovery — a brand-new module gets the next unused module number, never a reused or renumbered one, so once the single digits ran out (Tasks, 2026-09-11) the module part grew a digit rather than colliding. **An ID never changes once assigned**, even if the table is reordered or the tool is later removed — do not renumber existing rows to close a gap, and do not reuse a retired tool's ID for a different tool. Adding a tool to an existing module → give it the next unused 2-digit sequence number in that module (append at the end of that module's existing max, regardless of where the row is placed in the table). Removing a tool → delete its row; leave the gap in the sequence rather than shifting later IDs down.
- Adding, removing, or renaming a tool → add/remove/update its row (and the module's tool count in its section header and in the "60 tools" totals here and in README.md).
- Changing a tool's behavior (new params, different OWA action, altered response shape) → update its Description cell if it's no longer accurate, and reset its Manual QA status to `Pending` unless it's been re-verified.
- Running or receiving the result of a manual test against a live OWA mailbox → update that tool's Manual QA / Status cell to `OK` or `KO` (with a one-line note for `KO`), don't leave it stale at `Pending`.
- A tool becoming, or ceasing to be, a confirmed unfixable server-side failure (not merely `Pending`, and not a degraded-but-working case like `get_meeting_contacts`'s empty-result-plus-`warnings` behavior) → keep its Stability column cell (`Stable`/`Dev`) and `KNOWN_BUGGY_TOOLS` in `exchange_mcp/server.py` in sync with each other. `KNOWN_BUGGY_TOOLS` is what `--stable`/`EXCHANGE_MCP_STABLE` excludes from the MCP tool listing at startup.
- Landing an automated test for a tool or helper → update the Automated test column for the affected row(s) and the note in §4 if it was called out there as a gap. A new `tests/unit/` suite needs **no** CI change (`python -m tests.unit` discovers it) — just give it a `main() -> bool` and add it to the per-suite list in "Running" above. A unit suite for a helper shared by many tools (e.g. `folder_id_dict()`) belongs in the Automated test column of the rows where that helper's behavior is the row's own subject, not of every caller.
