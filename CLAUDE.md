# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Exchange MCP is a Model Context Protocol server for any Microsoft Exchange / OWA (Outlook Web Access) deployment. It gives LLM agents full access to email, calendar, directory, availability, and meeting analytics over the standard OWA JSON API. Works with any on-premise or hosted Exchange server that exposes OWA.

## Configuration

The server requires one environment variable, plus optional ones for the browser session:

- `EXCHANGE_OWA_URL` — Base URL of the OWA instance (e.g. `https://owa.example.com`)
- `EXCHANGE_MASTER_PASSWORD` — (optional) If set, the server logs in automatically at startup using stored encrypted credentials, blocking through 2FA before it starts serving tools.
- `EXCHANGE_BROWSER_PROFILE_DIR` — (optional) Path to the persistent Chromium profile directory. Defaults to `.browser-profile/` next to the package.
- `EXCHANGE_HEADLESS` — (optional) Set to `false`/`0` to run the browser with a visible window. Same effect as the `--show-browser` CLI flag (which takes precedence).
- `EXCHANGE_MCP_TRANSPORT` — (optional) `stdio` (default) or `http`. Same effect as `--transport`.
- `EXCHANGE_MCP_HOST` / `EXCHANGE_MCP_PORT` — (optional) Bind address for `--transport http`. Default `127.0.0.1:8765` — never bind non-loopback, the MCP endpoint has no auth of its own.

Any variable above can also be placed in a gitignored `.env.local` next to `pyproject.toml` (see `.env.local.example`); `server.py` loads it at startup without overriding variables already present in the environment. Useful when starting the server from a context with no shell to `export` into.

## Structure

- `login.py` — Browser-based login via 2FA, against the same persistent Chromium profile the server uses
- `exchange_mcp/` — MCP server package (30 tools)
  - `server.py` — FastMCP server with lifespan context; launches the browser and (if `EXCHANGE_MASTER_PASSWORD` is set) blocks on login before serving
  - `browser_session.py` — `BrowserSession`: one persistent Chromium context for the process's lifetime, reused by every OWA call
  - `owa_client.py` — OWA API client; delegates transport to `BrowserSession`, keeps the request/response/folder-resolution logic
  - `auth.py` — Login glue between the MCP tool and `BrowserSession`, plus credential encryption (reuses crypto from `login.py`). It's the *only* place inside the package that imports `login.py` — do the same anywhere else that needs those helpers (see note below), don't import `login` directly.
  - `tools/` — Tool modules: email, calendar, people, folders, availability, analytics, auth

## Running

```bash
export EXCHANGE_OWA_URL=https://owa.example.com

python login.py --setup       # One-time credential setup
python login.py               # Login (pre-warms the persistent browser profile)
pip install -e .               # Install MCP server
exchange-mcp-server            # Run MCP server (stdio transport, spawned per client session)
exchange-mcp-server --show-browser  # Same, with a visible browser window

# Persistent local server instead of per-session stdio spawn (start manually,
# no autostart mechanism — must already be running before a client connects):
exchange-mcp-server --transport http --port 8765

# Persistent local servet to use during smoke test
exchange-mcp-server --transport http --port 8765 --show-browser
```

Dependencies: `mcp`, `cryptography`, `playwright` (run `playwright install chromium` once). `mcp`'s `streamable-http` transport (`uvicorn`/`starlette`) is already a transitive dependency — no extra install needed for `--transport http`.

## Architecture

**Browser-per-call transport**: Every OWA call — not just login — goes through one persistent, real Chromium instance instead of a plain HTTP client, because OWA now requires signals (a fresh per-page CSRF canary, browser-like headers, a real TLS/JS fingerprint) that a hand-rolled `requests` session replaying exported cookies can't replicate. `BrowserSession` launches Chromium once via `launch_persistent_context` (on-disk profile, survives restarts and benefits from Microsoft's "stay signed in" cookie). Each tool call opens its own tab against that same context, performs its fetch, and closes the tab. A background thread with a dedicated asyncio loop hosts Playwright's async API; `OWAClient`/tool code call into it through plain synchronous methods.

**Session-based workflow**: `login.py` (CLI), the `login` MCP tool, or `EXCHANGE_MASTER_PASSWORD` at startup authenticate via browser-based 2FA directly on the persistent profile — there's no cookie file or in-memory cookie hand-off anymore; the browser profile itself is the session. The server starts even without a valid session (tolerant); the `login` tool can authenticate within the MCP session afterward.

**OWA JSON API pattern**:
1. Open a new browser tab, read the current `X-OWA-CANARY` cookie from the persistent context
2. `fetch()` the request from inside that tab (`page.evaluate`), capturing the real response via `page.expect_response` for status/headers/body
3. POST JSON to `$EXCHANGE_OWA_URL/owa/service.svc?action=<ACTION>`
4. Request bodies use EWS `__type` annotations (e.g. `"CalendarItem:#Exchange"`)
5. HTTP 401/440 = session expired → `OWAClient` calls `BrowserSession.ensure_logged_in()` (silent if the profile is still signed in, or using cached credentials) and retries once

**RequestServerVersion**: `Exchange2013` for reads, `V2017_08_18` for writes.

**Recovery**: if the browser process/context crashes, `BrowserSession` relaunches on the same profile directory and retries the call once. If the OWA session expires, `OWAClient` retries once after a re-login attempt.

**Encryption**: PBKDF2-HMAC-SHA256 (480,000 iterations) + AES-256-Fernet for stored credentials (`.credentials.enc`/`.salt`). Sessions no longer go through this — they live in the browser profile directory instead of an encrypted cookie file.

**Importing `login.py` from inside the package**: `login.py` lives at the repo root and isn't a packaged module (no `py-modules` entry in `pyproject.toml`), so `from login import ...` only resolves when the running process's own `sys.path` happens to include the repo root — true for `python login.py` or `python -m exchange_mcp.server` run from repo root, **false** for the installed `exchange-mcp-server` console-script entry point (its wrapper puts `Scripts/`/`bin/` on `sys.path[0]`, not the caller's cwd). `exchange_mcp/auth.py` works around this by inserting the repo root into `sys.path` before importing `login`; always import those helpers via `from exchange_mcp.auth import ...`, never `from login import ...` directly, or the import silently fails at runtime under the real entry point (caught by a broad `except` in `server.py`'s startup path, so it won't crash — it'll just skip auto-login and log `No module named 'login'`).

**Transport (`stdio` vs `http`)**: `main()` picks the transport via `--transport`/`EXCHANGE_MCP_TRANSPORT`. The lifespan that creates the `BrowserSession` runs exactly once per process either way — under `stdio` that process is spawned and killed per client session, so the warm browser/login is rebuilt every time; under `--transport http` the process is long-lived and the same `BrowserSession`/login is shared across every client connection that hits it, but it must be started manually — there is no autostart mechanism. Never bind `--host`/`EXCHANGE_MCP_HOST` off `127.0.0.1` — the MCP endpoint has no auth of its own, and FastMCP's `transport_security` (Host header validation) must stay enabled to block DNS-rebinding from other pages in the user's browser.

## Maintaining PROJECT_STATUS.md

[PROJECT_STATUS.md](PROJECT_STATUS.md) tracks, per MCP tool: migration status, automated-test coverage, and manual QA result (`Pending`/`OK`/`KO`). Keep it in sync as part of the same change, not as a follow-up:

- Adding, removing, or renaming a tool → add/remove/update its row (and the module's tool count in its section header and in the "30 tools" totals here and in README.md).
- Changing a tool's behavior (new params, different OWA action, altered response shape) → update its Description cell if it's no longer accurate, and reset its Manual QA status to `Pending` unless it's been re-verified.
- Running or receiving the result of a manual test against a live OWA mailbox → update that tool's Manual QA / Status cell to `OK` or `KO` (with a one-line note for `KO`), don't leave it stale at `Pending`.
- Landing an automated test for a tool or helper → update the Automated test column for the affected row(s) and the note in §4 if it was called out there as a gap.
