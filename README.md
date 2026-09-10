<!-- mcp-name: io.github.dianlight/owa-exchange-mcp -->

# OWA Exchange MCP Server

MCP (Model Context Protocol) server for any Microsoft Exchange / OWA (Outlook Web Access) deployment. Gives LLM agents access to email, calendar, directory search, folders, categories, availability, meeting analytics, and Copilot delegation via 46 tools.

Works with any on-premise or hosted Exchange server that exposes OWA.

## Quick Start

```bash
# Copy and edit the MCP config with your OWA URL
cp .mcp.json.example .mcp.json

# Install the MCP server
pip install -e .
```

There is no credential setup step. The first time the server starts it opens a
browser window on your OWA sign-in page — sign in there (2FA included) and the
session is saved in a persistent browser profile and reused from then on, across
restarts.

## Install

Two ways to run the server, depending on whether you want it spawned per session or always-on:

### Option A: stdio (spawned per client session)

The client starts and stops the process itself. Simple, but every new session
pays a cold Chromium start. Replace `https://owa.example.com` with your OWA URL.

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`),
**Cursor** (`.cursor/mcp.json`), or **Claude Code** (`.mcp.json`):

```json
{
  "mcpServers": {
    "exchange": {
      "type": "stdio",
      "command": "uvx",
      "args": ["exchange-mcp-server"],
      "env": {
        "EXCHANGE_OWA_URL": "https://owa.example.com"
      }
    }
  }
}
```

### Option B: streamable-http (persistent, recommended for Claude Cowork)

Run the server once as its own long-lived process, then point clients at its
URL instead of spawning it. The browser/login session stays warm across
sessions and can be shared by multiple client windows:

```bash
exchange-mcp-server --transport http --port 8765
```

Start it manually whenever you need it — there is no autostart mechanism;
the process must already be listening before a client tries to connect.

```json
{
  "mcpServers": {
    "exchange": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

`.mcp.json.example` in this repo ships with the Option B config — copy it to
`.mcp.json` and adjust the port if you changed it.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `EXCHANGE_OWA_URL` | Yes | Base URL of your OWA instance |
| `EXCHANGE_BROWSER_PROFILE_DIR` | No | Path to the persistent browser profile directory (default: `~/owa-mcp/.browser-profile`, or `<repo>/.browser-profile` when running from a source checkout) |
| `EXCHANGE_LOGIN_TIMEOUT` | No | Seconds the sign-in window waits for you before giving up (default `300`) |
| `EXCHANGE_HEADLESS` | No | Set to `false`/`0` to run the browser with a visible window (same effect as `--show-browser`) |
| `EXCHANGE_MCP_TRANSPORT` | No | `stdio` (default) or `http`. Same effect as `--transport`. |
| `EXCHANGE_MCP_HOST` | No | Bind host for `--transport http` (default `127.0.0.1` — keep it on loopback, see [Security](#security)) |
| `EXCHANGE_MCP_PORT` | No | Bind port for `--transport http` (default `8765`) |
| `EXCHANGE_MCP_STABLE` | No | Set to `true`/`1`/`yes` to exclude known-buggy tools from the MCP tool listing (same effect as `--stable`) |

Any of these can also live in a gitignored `.env.local` file next to
`pyproject.toml` (copy `.env.local.example`) — the server loads it at startup
without overriding variables already set in the environment. Useful when
starting the server from a context with no shell to `export` into.

## Architecture

Every OWA call — not just login — goes through one persistent, real Chromium
instance instead of a plain HTTP client, because OWA now requires signals
(a fresh per-page CSRF canary, browser-like headers, a real TLS/JS
fingerprint) that a hand-rolled HTTP session can no longer fake. The browser
launches once at server startup, backed by an on-disk profile so the session
(and Microsoft's "stay signed in" cookie) survives restarts. Each tool call
opens its own tab against that same session, does its work, and closes the
tab. If the browser crashes or the OWA session expires, it recovers
automatically on the next call.

The server itself runs over either transport (`--transport stdio`, the
default, or `--transport http`). The lifespan that creates the browser
session runs exactly once per process either way; over `http` that process
outlives any single client session, so the warm browser/login is shared
across every client connection instead of being rebuilt per session.

Pass `--show-browser` (or set `EXCHANGE_HEADLESS=false`) to run with a
visible window instead of headless, useful for watching the login flow or
debugging a stuck call.

## Login

The server holds **no credentials** — no stored password, no master password, no
setup step, no login script. The persistent browser profile *is* the session.

### How it works

1. On start, the server looks for its browser profile directory
   (`EXCHANGE_BROWSER_PROFILE_DIR`, else `~/owa-mcp/.browser-profile`). It reuses
   that profile if it exists and creates it if it doesn't.
2. If the profile is still signed in — live OWA cookies, Microsoft's "stay signed
   in" cookie, or an SSO session the web client can still use — the server just
   serves. Nothing is shown, nothing is asked.
3. If it isn't, the server **opens a visible browser window** on your OWA sign-in
   page and waits (default 300s, `EXCHANGE_LOGIN_TIMEOUT`). You sign in there:
   address, password, 2FA. Nothing is typed for you.
4. That window stays visible for the rest of the server's lifetime after a
   successful sign-in; restart the server to go back to headless.

### If you're not at the keyboard

The server keeps running. Tools return `"authorization_required": true` with a
`reason` and a `remediation` string, and the `login` tool reopens the sign-in
window whenever you're ready:

```
login()          # opens the window, returns immediately
                 # ... you sign in there ...
login()          # confirms the session is live
login(force=true)  # open the window even if the session looks fine (switch accounts)
```

`login` is deliberately two-call: a real sign-in takes minutes, longer than an
MCP client will hold a request open. `check_session` reports the same fields
without opening anything.

### When a sign-in doesn't complete

The sign-in page is read once, on timeout, to explain what happened —
`exchange_mcp/auth_errors.py` recognizes Entra ID `AADSTS` codes plus on-prem
ADFS/OWA wording, and maps them to a reason and a fix: expired or must-change
password, locked or disabled account, unrecognized account, denied MFA prompt,
MFA enrollment needed, conditional-access block. Anything unrecognized is
reported honestly as a plain timeout rather than guessed at.

Note that this is *diagnosis only*. The login window never aborts early on an
error message: you're sitting in front of it, so a mistyped password or an
accidentally denied push is something you just retry there.

## Tools (46)

### Email (14)
| Tool | Description |
|---|---|
| `get_emails` | List emails from a folder with filtering |
| `get_email` | Get full email content by ID |
| `search_emails` | Full-text search emails using AQS query syntax |
| `send_email` | Send a new email |
| `reply_email` | Reply to an email |
| `forward_email` | Forward an email |
| `delete_email` | Delete an email |
| `move_email` | Move email to another folder |
| `mark_email_read` | Mark email as read/unread |
| `download_attachments` | Download file attachments from an email |
| `get_email_links` | Extract hyperlinks from an email body |
| `assign_email_categories` | Tag emails with one or more categories |
| `remove_email_categories` | Remove categories from emails |
| `find_emails_by_category` | Find emails tagged with a given category |

### Calendar (10)
| Tool | Description |
|---|---|
| `get_calendar_events` | Get events in a date range |
| `create_meeting` | Create a meeting with attendees |
| `update_meeting` | Update an existing meeting |
| `cancel_meeting` | Cancel a meeting and notify attendees |
| `respond_to_meeting` | Accept, decline, or tentatively accept |
| `download_event_attachments` | Download file attachments from a calendar event |
| `get_event_links` | Extract hyperlinks from an event description |
| `assign_event_categories` | Tag calendar events with one or more categories |
| `remove_event_categories` | Remove categories from calendar events |
| `find_events_by_category` | Find calendar events tagged with a given category |

### Categories (4)
| Tool | Description |
|---|---|
| `list_categories` | List the master category list (name + color) |
| `create_category` | Create a new category on the master list |
| `rename_category` | Rename an existing category |
| `delete_category` | Delete a category from the master list |

### Directory (1)
| Tool | Description |
|---|---|
| `find_person` | Search people in Active Directory |

### Folders (7)
| Tool | Description |
|---|---|
| `get_folders` | List mail folders with unread counts |
| `create_folder` | Create a new mail folder |
| `rename_folder` | Rename an existing folder |
| `empty_folder` | Empty all items from a folder |
| `delete_folder` | Delete a mail folder |
| `move_folder` | Move a folder to a different parent |
| `check_session` | Check if the OWA session is authenticated |

### Availability (2)
| Tool | Description |
|---|---|
| `find_free_time` | Find free slots in your calendar |
| `find_meeting_time` | Find common free slots for multiple people |

### Analytics (2)
| Tool | Description |
|---|---|
| `get_meeting_stats` | Meeting count statistics for multiple people |
| `get_meeting_contacts` | Connection matrix — who you meet with most |

### Auth (1)
| Tool | Description |
|---|---|
| `login` | Authenticate to OWA (credential setup + 2FA login) |

### Copilot (5)
| Tool | Description |
|---|---|
| `ask_copilot` | Delegate a free-text question/instruction to Microsoft Copilot's chat pane |
| `summarize_email_thread` | Ask Copilot to summarize an email thread with action items |
| `draft_reply_with_copilot` | Ask Copilot to draft a reply to an email (returns text, doesn't send) |
| `coach_draft` | Ask Copilot's compose coaching for feedback on a draft reply |
| `meeting_prep` | Ask Copilot to prepare a briefing for an upcoming meeting |

Copilot tools only work against the modern Outlook backend (bearer auth mode)
and drive Copilot's chat pane via Playwright UI automation, since Copilot has
no documented API — see `exchange_mcp/tools/copilot.py` and PROJECT_STATUS.md
for the current (unverified, pending a live discovery spike) status.

## Files

```
exchange_mcp/
  server.py               # FastMCP server entry point
  browser_session.py      # Persistent Chromium context + interactive sign-in
  owa_client.py           # OWA API client (delegates transport to BrowserSession)
  auth_errors.py          # Diagnosis of a timed-out sign-in (reasons + remediation)
  tools/
    email.py              # Email tools
    calendar.py           # Calendar tools
    categories.py         # Master category list CRUD
    people.py             # Directory search
    folders.py            # Folder management & session check
    availability.py       # Free time / meeting time
    analytics.py          # Meeting stats & contacts
    auth.py               # Login tool (opens the sign-in window)
    copilot.py            # Copilot chat-pane delegation (UI automation)
tests/
  unit/                   # Pure-logic tests (no mailbox, no browser)
  smoke/                  # Live-mailbox end-to-end tests, one module per tool
pyproject.toml            # Package config
```

## Warning

Every Exchange / OWA deployment has its own authentication setup — 2FA (push notifications, TOTP, SMS), single-factor, SSO, smartcards. Because the server no longer types anything into the sign-in page (you do, in a real browser window), whatever your deployment asks for should work as-is. What *is* deployment-specific is how the session is detected afterwards: `BrowserSession._async_has_active_session` looks for an `X-OWA-CANARY` cookie (classic OWA) or a Bearer token the modern Outlook web client can mint (see `auth_mode`). A deployment that signals its session some other way would need that check extended.

## Security

- **No credentials are stored anywhere** — no password file, no master password,
  no keychain entry. The only secret at rest is the browser profile itself.
- The browser profile directory holds the live session the same way a
  logged-in browser normally would; treat it like a browser profile
  (don't share it, exclude it from backups you'd share)
- **If running with `--transport http`**: bind stays on `127.0.0.1` by
  default — never set `EXCHANGE_MCP_HOST`/`--host` to `0.0.0.0` or a LAN
  address, that would expose full mailbox access to your network with no
  authentication of its own. Keep FastMCP's built-in `transport_security`
  (Host header validation) enabled; it's what stops an unrelated web page
  in your regular browser from reaching `localhost:8765` via DNS rebinding.
  Any local process that can reach the port has the same mailbox access a
  spawned stdio process had before — this is a longer-lived process, not a
  wider trust boundary, but it's worth being deliberate about.
- `.env.local` holds only non-secret configuration now (OWA URL, port, profile
  path). It's still gitignored.
