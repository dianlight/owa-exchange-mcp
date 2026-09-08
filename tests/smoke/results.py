"""Append-only result log for smoke test runs, used to update PROJECT_STATUS.md."""

import datetime
import json
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / ".state"
LOG_FILE = STATE_DIR / "results.jsonl"


def record(tool: str, args: dict, outcome: str, note: str = "") -> dict:
    """outcome should be one of OK, TOOL_ERROR, EXCEPTION."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "tool": tool,
        "args": args,
        "outcome": outcome,
        "note": str(note)[:300],
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{outcome}] {tool}({args})  {entry['note']}")
    return entry


def is_error_payload(parsed) -> str | None:
    """Return an error note if `parsed` (a tool's JSON result) signals failure."""
    if isinstance(parsed, dict):
        if parsed.get("_exception"):
            return f"exception: {parsed['_exception']}"
        if parsed.get("_transport_error"):
            return f"transport error: {parsed.get('raw', '')[:200]}"
        if "error" in parsed:
            return str(parsed["error"])[:200]
    return None
