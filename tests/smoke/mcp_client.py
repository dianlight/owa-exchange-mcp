"""Shared MCP client helpers for the smoke suite.

Every test module connects through `session()`, which ensures the shared
HTTP server is up (starting it if needed, reusing it otherwise) and yields
a live ClientSession. `call()` unwraps the tool's JSON text response the
same way the ad-hoc scripts in scripts/ did, so tool-level {"error": ...}
payloads and MCP-level isError are both surfaced without raising.
"""

import asyncio
import json
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tests.smoke.server_manager import SERVER_URL, ensure_server

# The first tool call right after a cold server start can race the
# background browser launch (see server.py's _startup task) and fail
# transiently; retry a couple of times before giving up.
FIRST_CALL_RETRIES = 3
FIRST_CALL_RETRY_DELAY = 5


@asynccontextmanager
async def session():
    ensure_server()
    async with streamablehttp_client(SERVER_URL) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def call_args(s: ClientSession, tool: str, args: dict):
    """Same as call(), but taking the tool's arguments as an explicit dict.

    Needed whenever a tool has a parameter that collides with call()'s own
    keyword parameters - `create_folder`'s `name`, for instance, makes
    `call(s, "create_folder", name=...)` raise "got multiple values for
    argument 'name'". Prefer this over re-inlining the raw call_tool
    plumbing in a test module.
    """
    last_exc = None
    for attempt in range(FIRST_CALL_RETRIES):
        try:
            result = await s.call_tool(tool, args)
            break
        except Exception as e:
            last_exc = e
            if attempt == FIRST_CALL_RETRIES - 1:
                return {"_exception": str(e)}
            await asyncio.sleep(FIRST_CALL_RETRY_DELAY)
    else:
        return {"_exception": str(last_exc)}

    text = "".join(getattr(b, "text", "") for b in result.content)
    if result.isError:
        return {"_transport_error": True, "raw": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_non_json": True, "raw": text}


async def call(s: ClientSession, name: str, **args):
    """Call an MCP tool and return its parsed JSON payload (or raw text/dict wrapper)."""
    return await call_args(s, name, args)


def run(coro):
    """Convenience for test scripts' `if __name__ == "__main__"` entry points."""
    return asyncio.run(coro)
