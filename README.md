<!-- mcp-name: io.github.dianlight/owa-exchange-mcp -->

# OWA Exchange MCP Server

MCP (Model Context Protocol) server for any Microsoft Exchange / OWA (Outlook Web Access) deployment. Gives LLM agents access to email, calendar, directory search, folders, categories, availability, and meeting analytics via 40 tools.

Works with any on-premise or hosted Exchange server that exposes OWA.

## Quick Start

```bash
# Copy and edit the MCP config with your OWA URL
cp .mcp.json.example .mcp.json

# One-time: set up encrypted credentials
python3 login.py --setup

# Login (opens headless browser, 2FA approval required)
python3 login.py

# Install the MCP server
pip install -e .
```

## Install

Two ways to run the server, depending on whether you want it spawned per session or always-on:

### Option A: stdio (spawned per client session)

The client starts and stops the process itself. Simple, but every new session
pays a cold Chromium start (and an interactive 2FA wait, if configured).
Replace `https://owa.example.com` with your OWA URL.

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
| `EXCHANGE_MASTER_PASSWORD` | No | If set, the server logs in automatically at startup using stored encrypted credentials (waits through 2FA before serving) |
| `EXCHANGE_BROWSER_PROFILE_DIR` | No | Path to the persistent browser profile directory (default: `.browser-profile/` next to the package) |
| `EXCHANGE_HEADLESS` | No | Set to `false`/`0` to run the browser with a visible window (same effect as `--show-browser`) |
| `EXCHANGE_MCP_TRANSPORT` | No | `stdio` (default) or `http`. Same effect as `--transport`. |
| `EXCHANGE_MCP_HOST` | No | Bind host for `--transport http` (default `127.0.0.1` — keep it on loopback, see [Security](#security)) |
| `EXCHANGE_MCP_PORT` | No | Bind port for `--transport http` (default `8765`) |

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

### Option A: Automatic at startup (recommended)

Set `EXCHANGE_MASTER_PASSWORD` in your MCP client config's `env` block. The
server decrypts stored credentials and logs in before it starts serving
tool calls (blocking through any 2FA approval, up to ~90 seconds). Run
`python3 login.py --setup` once beforehand to store the encrypted
credentials.

### Option B: Via MCP tool

The `login` tool handles credential setup and authentication within the MCP session — no separate terminal needed.

First time (setup + login):
```
login(master_password="...", username="user@example.com", password="...")
```

Subsequent logins (decrypts stored credentials):
```
login(master_password="...")
```

### Option C: Via CLI

```bash
python3 login.py --setup   # First time: save encrypted credentials
python3 login.py            # Login with 2FA
```

All methods drive the same persistent browser profile: submit credentials,
wait for 2FA approval (up to 90 seconds), and leave the session live in that
profile for the MCP server to pick up. Credentials are encrypted at rest
with AES-256 (PBKDF2 key derivation, 480k iterations); the browser profile
itself holds the live session (cookies) the way a real browser would.

## Tools (40)

### Email (13)
| Tool | Description |
|---|---|
| `get_emails` | List emails from a folder with filtering |
| `get_email` | Get full email content by ID |
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
| `get_calendar_events` | Get events in a date range (supports recurring expansion) |
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

## Files

```
login.py                  # Browser-based 2FA login (standalone CLI)
exchange_mcp/
  server.py               # FastMCP server entry point
  browser_session.py      # Persistent Chromium context shared by every OWA call
  owa_client.py           # OWA API client (delegates transport to BrowserSession)
  auth.py                 # Async login logic (shared by MCP tool)
  tools/
    email.py              # Email tools
    calendar.py           # Calendar tools
    categories.py         # Master category list CRUD
    people.py             # Directory search
    folders.py            # Folder management & session check
    availability.py       # Free time / meeting time
    analytics.py          # Meeting stats & contacts
    auth.py               # Login tool
pyproject.toml            # Package config
```

## Warning

Every Exchange / OWA deployment has its own authentication setup — some require 2FA (push notifications, TOTP, SMS), others use single-factor login or SSO. The login logic in this project (`login.py` and `exchange_mcp/auth.py`) is written for a specific 2FA flow (mobile push approval). If your OWA server uses a different 2FA method or no 2FA at all, you will need to modify or remove the login logic to match your environment.

## Security

- Credentials encrypted at rest with AES-256-Fernet (PBKDF2, 480,000 iterations)
- Master password never stored
- Credential files have `0600` permissions
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
- `.env.local` holds `EXCHANGE_MASTER_PASSWORD` in plaintext on disk — same
  trade-off as passing it via a client's `env` block today. It's gitignored;
  don't check it in or share it.
