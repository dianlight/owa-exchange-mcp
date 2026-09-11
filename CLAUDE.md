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
- `EXCHANGE_DISCOVERY_DIR` — (optional) Where capability-discovery captures are written. Default comes from `discovery_session.default_discovery_dir()` and mirrors the profile-dir split: `<repo>/.discovery-sessions` in a source checkout, `~/owa-mcp/discovery-sessions` for an installed package. Gitignored — a capture holds real mailbox metadata (see "Capability discovery" below).

**Version**: `exchange_mcp/__version__` is the single source of truth — `pyproject.toml` reads it via `[tool.setuptools.dynamic] version = {attr = ...}`, and the startup banner prints it. Don't hardcode a version in `pyproject.toml`: an editable install doesn't refresh its metadata when the tree changes, so `importlib.metadata.version()` goes stale and a banner that misreports its own version is worse than no banner. `server.json`'s two `version` fields still have to be bumped by hand (it's a published registry manifest and can't read Python attributes).

Any variable above can also be placed in a gitignored `.env.local` next to `pyproject.toml` (see `.env.local.example`); `server.py` loads it at startup without overriding variables already present in the environment. Useful when starting the server from a context with no shell to `export` into.

## Structure

- `exchange_mcp/` — MCP server package (60 tools)
  - `server.py` — FastMCP server with lifespan context; launches the browser on the persistent profile and, if that profile isn't signed in, opens a visible sign-in window (off the handshake path — see "Authentication" below)
  - `browser_session.py` — `BrowserSession`: one persistent Chromium context for the process's lifetime, reused by every OWA call
  - `owa_client.py` — OWA API client; delegates transport to `BrowserSession`, keeps the request/response/folder-resolution logic
  - `auth_errors.py` — Pure diagnosis of a *timed-out* interactive sign-in: reason codes, the AADSTS/page-text/URL hint tables, per-reason remediation text, and `AuthenticationRequiredError`. Imports nothing else from the package (no Playwright) so it stays unit-testable — see "Authentication" below.
  - `discovery_session.py` — `DiscoveryRecorder`: a *second*, independent Chromium on a throwaway profile, visible and driven by the user, recording every API call and UI action. Shares nothing with `BrowserSession` but the OWA URL — see "Capability discovery" below.
  - `capability_inventory.py` — What this server already implements, derived by `ast`-scanning `tools/*.py` and `owa_client.py` for transport call sites, plus PROJECT_STATUS.md for the ID-numbering state. No Playwright, unit-testable.
  - `capability_classify.py` — Verdicts and implementation proposals for a recorded capture. Pure logic; the domain knowledge lives in two keyword tables, correctable in one place like `auth_errors.py`'s.
  - `tools/` — Tool modules: email, calendar, categories, people, folders, availability, analytics, auth, copilot, tasks, discovery
- `tests/unit/` — Pure-logic tests, no live mailbox / browser / `EXCHANGE_OWA_URL` needed (`python -m tests.unit.test_auth_errors`, `python -m tests.unit.test_recurrence_expansion`, `python -m tests.unit.test_capability_classify`). Separate from `tests/smoke/`, which is live-mailbox end-to-end.
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

# Pure-logic tests (no mailbox, no browser, no EXCHANGE_OWA_URL)
python -m tests.unit.test_auth_errors
python -m tests.unit.test_recurrence_expansion
python -m tests.unit.test_capability_classify

# Live-mailbox smoke tests: one module per tool group, run individually.
# The harness starts its own server on 127.0.0.1:8765 if nothing is listening
# there; set EXCHANGE_SMOKE_HOST/EXCHANGE_SMOKE_PORT to reuse a server that is
# already running instead — two servers can't share one browser profile
# directory (Chromium holds an exclusive lock on it).
python -m tests.smoke.tests.test_copilot
EXCHANGE_SMOKE_PORT=8767 python -m tests.smoke.tests.test_copilot
```

There is no credential setup step and no login CLI: the first start opens a browser
window and you sign in there. See "Authentication" below.

Dependencies: `mcp`, `playwright` (run `playwright install chromium` once). `mcp`'s `streamable-http` transport (`uvicorn`/`starlette`) is already a transitive dependency — no extra install needed for `--transport http`.

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

**RequestServerVersion**: `Exchange2013` for reads, `V2017_08_18` for writes.

**Recovery**: if the browser process/context crashes, `BrowserSession` relaunches on the same profile directory and retries the call once. If the OWA session expires, `OWAClient` retries once after a silent re-auth attempt — see "Authentication" above for what happens when that can't succeed.

**Transport (`stdio` vs `http`)**: `main()` picks the transport via `--transport`/`EXCHANGE_MCP_TRANSPORT`. The lifespan that creates the `BrowserSession` runs exactly once per process either way — under `stdio` that process is spawned and killed per client session, so the warm browser/login is rebuilt every time; under `--transport http` the process is long-lived and the same `BrowserSession`/login is shared across every client connection that hits it, but it must be started manually — there is no autostart mechanism. Never bind `--host`/`EXCHANGE_MCP_HOST` off `127.0.0.1` — the MCP endpoint has no auth of its own, and FastMCP's `transport_security` (Host header validation) must stay enabled to block DNS-rebinding from other pages in the user's browser.

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
and the `*_folder` tools cover it — except *creating* one, since `create_folder` hardcodes
`FolderClass: "IPF.Note"` (see PROJECT_STATUS.md §4).

**Copilot tools (`tools/copilot.py`)**: unlike every other tool module, Copilot has no documented API to call — there is no EWS action, no REST endpoint, nothing to POST. These tools instead drive Copilot's own chat pane inside the modern Outlook web client directly via Playwright UI automation (`BrowserSession`'s Copilot section: `_async_copilot_locate_pane`/`_open_pane`/`_submit`/`_wait_and_read`/`_async_copilot_ask`/`copilot_ask()`), the same "automate OWA's own web UI" escape hatch already used for the calendar category write-path (`_set_event_categories`, see PROJECT_STATUS.md #208/#209) when no API exists. Only available in `bearer` auth mode (modern Outlook) — raises `BearerModeRequiredError` on classic canary-cookie OWA, the same exception `find_people`/`post_substrate` use for their own modern-backend-only surfaces. Because there's no DOM/API reference to build against, every selector, the generation-complete polling heuristic, and the item-grounding deep-link URL shape are best-guess placeholders pending a live discovery spike (`--show-browser` inspection of a real Copilot pane). The 2026-09-11 smoke run ([tests/smoke/tests/test_copilot.py](tests/smoke/tests/test_copilot.py)) confirmed that they are in fact wrong: all five tools fail on a live bearer-mode tenant, in two distinct ways (see PROJECT_STATUS.md's Copilot rows #901-905, now `KO`, and the §1 update). Don't expect any of these tools to work until the spike has corrected `browser_session.py` and that test passes.

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
  `tests/unit/test_capability_classify.py` asserts each of those directly rather than trusting
  them.
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
- Landing an automated test for a tool or helper → update the Automated test column for the affected row(s) and the note in §4 if it was called out there as a gap.
