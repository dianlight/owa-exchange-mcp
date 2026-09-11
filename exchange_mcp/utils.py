"""Shared utility functions for the Exchange MCP server.

Extracts duplicated helpers from the standalone scripts:
html_to_text, date/time formatting and parsing.
"""

import html
import re
from datetime import datetime


def html_to_text(html_content: str) -> str:
    """Convert HTML to plain text.

    Strips scripts, styles, converts <br>/<p>/<div> to newlines,
    removes remaining tags, and unescapes HTML entities.
    """
    if not html_content:
        return ""
    text = re.sub(
        r"<script[^>]*>.*?</script>", "", html_content, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(
        r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<p[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<div[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract_links_from_html(html_content: str) -> list[dict]:
    """Extract hyperlinks from HTML content.

    Finds <a href="...">text</a> patterns, excludes mailto:, cid:,
    javascript:, and fragment-only (#) links. Deduplicates by URL.

    Returns list of {url, text} dicts.
    """
    if not html_content:
        return []

    # Match <a ...href="URL"...>text</a>
    pattern = re.compile(
        r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )

    seen: set[str] = set()
    links: list[dict] = []

    for url_raw, text_raw in pattern.findall(html_content):
        url = html.unescape(url_raw).strip()

        # Skip non-http links
        if url.startswith(("mailto:", "cid:", "javascript:")) or url == "#":
            continue
        # Skip fragment-only links
        if url.startswith("#"):
            continue

        if url in seen:
            continue
        seen.add(url)

        # Clean link text: strip tags and whitespace
        text = re.sub(r"<[^>]+>", "", text_raw)
        text = html.unescape(text).strip()

        links.append({"url": url, "text": text})

    return links


def format_datetime(dt_str: str) -> str:
    """Format an ISO datetime string as 'YYYY-MM-DD HH:MM'.

    Strips timezone suffixes (Z, +offset) for cleaner display.
    """
    if not dt_str:
        return ""
    if "T" in dt_str:
        date_part, time_part = dt_str.split("T", 1)
        time_part = time_part.split("Z")[0].split("+")[0]
        return f"{date_part} {time_part[:5]}"
    return dt_str


def format_date(dt_str: str) -> str:
    """Extract the date portion from an ISO datetime string."""
    if not dt_str:
        return ""
    if "T" in dt_str:
        return dt_str.split("T")[0]
    return dt_str


def parse_date(date_str: str) -> datetime:
    """Parse a date string in common formats.

    Supports: YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY, MM/DD/YYYY.
    """
    formats = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse date: {date_str}")


def parse_iso_datetime(dt_str: str) -> datetime:
    """Parse an ISO datetime string to a naive datetime.

    Handles both 'YYYY-MM-DDTHH:MM:SS' and 'YYYY-MM-DD' formats,
    stripping any timezone suffix.
    """
    if "T" in dt_str:
        clean = dt_str.split("Z")[0].split("+")[0]
        return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
    return datetime.strptime(dt_str, "%Y-%m-%d")


def format_attendee(name: str, email: str) -> str:
    """Format an attendee as 'Name <email>' or just the email."""
    if name and email and not email.startswith("/O="):
        return f"{name} <{email}>"
    return name or email or ""


# ------------------------------------------------------------------
# Per-item failure classification
# ------------------------------------------------------------------
#
# Tools that act on a list of item_ids report each failure with a stable
# `error_code` alongside the raw server text, so a caller can branch on the
# *kind* of failure instead of substring-matching an HTTP 500 message. That
# distinction is what lets a client skill decide between "retry", "re-list the
# folder", and "fall back to another connector" -- pattern-matching a .NET
# exception name is not a contract anyone should be forced to rely on.
#
# Like auth_errors.py's tables, the domain knowledge lives here in one
# correctable place rather than being spread across the tool modules.

ITEM_NOT_SERIALIZABLE = "item_not_serializable"
ITEM_NOT_FOUND = "item_not_found"
ITEM_ACCESS_DENIED = "item_access_denied"
ITEM_READ_FAILED = "item_read_failed"

# Matched case-insensitively against the whole error string (which normally
# carries both the x-owa-error header's .NET exception name and a body snippet).
_ITEM_ERROR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (ITEM_NOT_SERIALIZABLE, ("serializationexception",)),
    (ITEM_NOT_FOUND, ("erroritemnotfound", "the specified object was not found")),
    (ITEM_ACCESS_DENIED, ("erroraccessdenied", "access is denied")),
)

_ITEM_ERROR_REMEDIATION = {
    ITEM_NOT_SERIALIZABLE: (
        "OWA's own serialiser faults while writing the response when the full "
        "property set is requested for this item -- observed on "
        "MeetingRequestMessage items. The item_id, the session and every write "
        "path are fine: a narrow GetItem shape (IdOnly plus named properties) "
        "reads the same item successfully. Request only the fields you need."
    ),
    ITEM_NOT_FOUND: (
        "No item exists at that ItemId any more -- it was moved or deleted. "
        "Re-list the folder to get current ids."
    ),
    ITEM_ACCESS_DENIED: (
        "The signed-in mailbox is not permitted to read this item."
    ),
    ITEM_READ_FAILED: "",
}


def classify_item_error(message: str) -> str:
    """Map a raw per-item failure message to a stable error code.

    Returns ITEM_READ_FAILED for anything unrecognised rather than guessing:
    an honest generic code is more useful to a caller than a wrong specific
    one, and callers are told to treat unknown codes as opaque.
    """
    lowered = (message or "").lower()
    for code, hints in _ITEM_ERROR_HINTS:
        if any(hint in lowered for hint in hints):
            return code
    return ITEM_READ_FAILED


def item_error(item_id: str, message: str) -> dict:
    """Build the per-item failure dict used in bulk-tool `failed` lists."""
    code = classify_item_error(message)
    failure = {"item_id": item_id, "error": message, "error_code": code}
    remediation = _ITEM_ERROR_REMEDIATION.get(code)
    if remediation:
        failure["hint"] = remediation
    return failure
