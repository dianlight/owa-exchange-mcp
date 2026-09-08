"""Quick manual sanity check: exercises every read-only tool in one pass
against an already-running exchange-mcp-server (streamable-http, port
8765) and prints a pass/fail summary.

Unlike tests/smoke/ (one module per tool, each independently repeatable
and tracked in PROJECT_STATUS.md), this script is a single unstructured
run through all read-only tools for a fast eyeball check - it doesn't
start/stop the server itself (assumes one is already up) and its
per-tool results aren't recorded anywhere durable. Kept for quick manual
use; tests/smoke/ is the source of truth for tool status.
"""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8765/mcp"

results = []


def record(name, args, outcome, note=""):
    results.append({"tool": name, "args": args, "outcome": outcome, "note": note})
    print(f"[{outcome}] {name}({args})  {note}")


async def call(session, name, args):
    try:
        res = await session.call_tool(name, args)
        text = ""
        for c in res.content:
            if hasattr(c, "text"):
                text += c.text
        if res.isError:
            record(name, args, "ERROR", text[:200])
            return None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        if isinstance(parsed, dict) and "error" in parsed:
            record(name, args, "TOOL_ERROR", str(parsed["error"])[:200])
            return parsed
        record(name, args, "OK", text[:150])
        return parsed
    except Exception as e:
        record(name, args, "EXCEPTION", str(e)[:200])
        return None


async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            session_info = await call(session, "check_session", {})
            self_mailbox = ""
            if isinstance(session_info, dict):
                self_mailbox = session_info.get("mailbox", "")

            await call(session, "get_folders", {"parent_folder_id": "msgfolderroot", "recursive": False})

            emails = await call(session, "get_emails", {"folder": "Inbox", "limit": 3})
            item_id = None
            if isinstance(emails, dict):
                items = emails.get("emails") or []
                if items:
                    item_id = items[0].get("item_id")

            if item_id:
                await call(session, "get_email", {"item_id": item_id})
                await call(session, "get_email_links", {"item_id": item_id})
                await call(session, "download_attachments", {"item_id": item_id, "target_folder": "./_qa_attachments"})
            else:
                print("[SKIP] get_email / get_email_links / download_attachments -- no item_id from get_emails")

            from datetime import date, timedelta
            today = date.today()
            start = str(today)
            end = str(today + timedelta(days=7))

            events = await call(session, "get_calendar_events", {"start_date": start, "end_date": end})
            event_id = None
            if isinstance(events, list) and events:
                event_id = events[0].get("item_id")

            if event_id:
                await call(session, "get_event_links", {"item_id": event_id})
                await call(session, "download_event_attachments", {"item_id": event_id, "target_folder": "./_qa_attachments"})
            else:
                print("[SKIP] get_event_links / download_event_attachments -- no item_id with events in range")

            people_query = self_mailbox or "a"
            person_results = await call(session, "find_person", {"query": people_query})

            await call(session, "find_free_time", {"start_date": start, "end_date": end})

            stats_person = people_query
            if isinstance(person_results, list) and person_results:
                stats_person = person_results[0].get("email") or people_query

            await call(session, "get_meeting_stats", {"people": stats_person, "start_date": start, "end_date": end})
            await call(session, "find_meeting_time", {"emails": stats_person, "start_date": start, "end_date": end})
            await call(session, "get_meeting_contacts", {"start_date": start, "end_date": end, "top_n": 5})

    print("\n=== SUMMARY ===")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
