"""Email tools for the Exchange MCP server.

Provides tools for reading, sending, replying, forwarding, and managing
emails via the OWA Exchange API.
"""

import json
import re
import shlex

from mcp.server.fastmcp import Context

from exchange_mcp.server import mcp, AppContext
from exchange_mcp.owa_client import OWAClient, SessionExpiredError
from exchange_mcp.utils import (
    ITEM_NOT_SERIALIZABLE,
    classify_item_error,
    extract_links_from_html,
    html_to_text,
    item_error,
)


def _get_client(ctx: Context) -> OWAClient:
    """Extract the OWAClient from the MCP lifespan context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _get_change_key(client: OWAClient, item_id: str) -> str | None:
    """Fetch the ChangeKey for an item via GetItem (IdOnly).

    OWA requires the ChangeKey on write operations like reply/forward.
    """
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "IdOnly",
            },
            "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
        },
    }
    data = client.request("GetItem", payload)
    for msg in client.extract_items(data):
        if "Items" in msg:
            for item in msg["Items"]:
                return item.get("ItemId", {}).get("ChangeKey")
    return None


def _extract_conversation_summary(conv: dict) -> dict:
    """Extract a summary dict from a FindConversation result item (one row per thread).

    item_ids are scoped to the folder being listed; the API doesn't document
    whether they're oldest-first or newest-first, so item_id below is a
    best-effort "most recent message in this thread" pointer (last entry).
    """
    item_ids = conv.get("ItemIds") or conv.get("GlobalItemIds") or []
    unread = conv.get("UnreadCount", 0)

    return {
        "conversation_id": conv.get("ConversationId", {}).get("Id", ""),
        "subject": conv.get("ConversationTopic") or "(No subject)",
        "senders": conv.get("UniqueSenders", []),
        "date": conv.get("LastDeliveryTime", ""),
        "is_read": unread == 0,
        "unread_count": unread,
        "message_count": conv.get("MessageCount", 0),
        "has_attachments": conv.get("HasAttachments", False),
        "item_id": item_ids[-1].get("Id", "") if item_ids else "",
        "item_ids": [i.get("Id", "") for i in item_ids],
        "size": conv.get("Size", 0),
        "categories": conv.get("Categories", []),
        "importance": conv.get("Importance", "Normal"),
        "preview": conv.get("Preview", ""),
        "flag_status": (conv.get("Flag") or {}).get("FlagStatus", "NotFlagged"),
    }


def _get_item_details(client: OWAClient, item_id: str) -> dict:
    """Get full email details (body, recipients, attachments) via GetItem."""
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2017_08_18",
        },
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "AllProperties",
                "BodyType": "HTML",
            },
            "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
        },
    }

    data = client.request("GetItem", payload)
    result = {
        "item_id": item_id,
        "subject": "",
        "from": "",
        "from_name": "",
        "date": "",
        "to": [],
        "cc": [],
        "bcc": [],
        "body": "",
        "body_type": "Text",
        "is_read": False,
        "has_attachments": False,
        "has_links": False,
        "importance": "Normal",
        "attachments": [],
        "categories": [],
        "flag_status": "NotFlagged",
    }

    for msg in client.extract_items(data):
        if "Items" not in msg:
            continue
        for item in msg["Items"]:
            result["subject"] = item.get("Subject", "(No subject)")
            result["categories"] = item.get("Categories", [])

            # Sender
            from_data = item.get("From", {}).get("Mailbox", {})
            if not from_data:
                from_data = item.get("Sender", {}).get("Mailbox", {})
            result["from"] = from_data.get("EmailAddress", "")
            result["from_name"] = from_data.get("Name", "")

            result["date"] = item.get(
                "DateTimeSent",
                item.get("DateTimeReceived", item.get("DateTimeCreated", "")),
            )
            result["is_read"] = item.get("IsRead", False)
            result["has_attachments"] = item.get("HasAttachments", False)
            result["importance"] = item.get("Importance", "Normal")
            result["flag_status"] = (item.get("Flag") or {}).get("FlagStatus", "NotFlagged")

            # Body
            body_val = item.get("Body", {}).get("Value", "")
            body_type = item.get("Body", {}).get("BodyType", "Text")
            if body_type == "HTML":
                result["has_links"] = bool(extract_links_from_html(body_val))
                result["body"] = html_to_text(body_val)
            else:
                result["body"] = body_val
            result["body_type"] = body_type

            # To recipients
            for r in item.get("ToRecipients", []):
                name = r.get("Name", "")
                addr = r.get("EmailAddress", "")
                if name and addr:
                    result["to"].append(f"{name} <{addr}>")
                elif addr:
                    result["to"].append(addr)

            # CC recipients
            for r in item.get("CcRecipients", []):
                name = r.get("Name", "")
                addr = r.get("EmailAddress", "")
                if name and addr:
                    result["cc"].append(f"{name} <{addr}>")
                elif addr:
                    result["cc"].append(addr)

            # BCC recipients
            for r in item.get("BccRecipients", []):
                name = r.get("Name", "")
                addr = r.get("EmailAddress", "")
                if name and addr:
                    result["bcc"].append(f"{name} <{addr}>")
                elif addr:
                    result["bcc"].append(addr)

            # Attachments (with IDs for download)
            for att in item.get("Attachments", []):
                result["attachments"].append(
                    {
                        "name": att.get("Name", ""),
                        "size": att.get("Size", 0),
                        "content_type": att.get("ContentType", ""),
                        "attachment_id": att.get("AttachmentId", {}).get("Id", ""),
                        "is_inline": att.get("IsInline", False),
                    }
                )

            # Meeting-specific fields
            item_type = item.get("__type", "")
            if any(
                t in item_type
                for t in ("MeetingRequest", "MeetingResponse", "MeetingCancellation", "CalendarItem")
            ):
                result["location"] = item.get(
                    "Location",
                    item.get("EnhancedLocation", {}).get("DisplayName", ""),
                )
                result["start"] = item.get("Start", "")
                result["end"] = item.get("End", "")
                result["required_attendees"] = []
                result["optional_attendees"] = []
                for a in item.get("RequiredAttendees", []):
                    mb = a.get("Mailbox", {})
                    name = mb.get("Name", "")
                    addr = mb.get("EmailAddress", "")
                    if name and addr:
                        result["required_attendees"].append(f"{name} <{addr}>")
                    elif addr:
                        result["required_attendees"].append(addr)
                for a in item.get("OptionalAttendees", []):
                    mb = a.get("Mailbox", {})
                    name = mb.get("Name", "")
                    addr = mb.get("EmailAddress", "")
                    if name and addr:
                        result["optional_attendees"].append(f"{name} <{addr}>")
                    elif addr:
                        result["optional_attendees"].append(addr)

            return result

    return result


def _try_get_item_details(client: OWAClient, item_id: str) -> tuple[dict | None, str | None]:
    """`_get_item_details` that reports failure instead of raising.

    Returns (details, None) or (None, error_note).

    Some real messages can't be fetched *at this shape*: with
    `BaseShape: "AllProperties"` OWA's own GetItem throws
    `System.Runtime.Serialization.SerializationException` (HTTP 500) — observed
    on MeetingRequestMessage items in a live mailbox, and a fault in the
    server's own response serialisation, so no request-side change fixes it.
    Any loop over several item_ids therefore has to be able to skip one bad
    item, because letting it propagate takes down the whole batch: a listing
    loses every other row.

    What this does *not* mean is that such an item is unreachable. A narrower
    read of the same item succeeds — see `_get_item_categories`, which is why
    the category tools no longer go through here at all. Anything that needs
    only a few named fields should ask for those fields instead of degrading.

    SessionExpiredError is deliberately re-raised — that isn't a per-item
    problem and the caller's retry/re-login path should see it.
    """
    try:
        return _get_item_details(client, item_id), None
    except SessionExpiredError:
        raise
    except Exception as e:
        return None, str(e)


def _get_item_categories(client: OWAClient, item_id: str) -> list[str]:
    """Read *only* the Categories of one item, via the narrowest GetItem shape.

    The category write tools need the current Categories to merge against, and
    reading them with `_get_item_details`' `BaseShape: "AllProperties"` is what
    made them unusable on meeting invites: OWA's own serialiser faults (HTTP 500
    `System.Runtime.Serialization.SerializationException`) partway through
    writing the response for a `MeetingRequestMessageType` item when the full
    property set is requested. Confirmed live 2026-09-11 — the truncated 500
    body breaks off inside the item's own `__type` marker, i.e. the server
    failed while *serialising its answer*, not while parsing our request.

    An `IdOnly` + named-`Categories` shape reads the same item without faulting
    (the same narrow shape `get_email_links` already relies on), so this is not
    a workaround for a broken item — it is simply not asking for the property
    that OWA can't render. Also strictly cheaper: a bulk tag operation no longer
    drags a full body, recipient list and attachment set over the wire per item.

    Returns [] for an item with no categories; raises for a real read failure so
    the caller can report it per-item instead of silently writing over unknown
    state.
    """
    payload = {
        "__type": "GetItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "GetItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "IdOnly",
                "AdditionalProperties": [
                    {"__type": "PropertyUri:#Exchange", "FieldURI": "Categories"},
                ],
            },
            "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
        },
    }

    data = client.request("GetItem", payload)
    for msg in client.extract_items(data):
        if msg.get("ResponseClass") == "Error":
            raise RuntimeError(msg.get("MessageText", "GetItem failed."))
        for item in msg.get("Items", []):
            return list(item.get("Categories") or [])

    # One ItemId in must produce one item (or an error message) out. Anything
    # else means we'd be merging against categories we never actually read.
    raise RuntimeError("GetItem returned no item for this ItemId.")


def _bulk_result(action: str, updated: list[str], failed: list[dict], requested: int) -> dict:
    """Summarise a per-item bulk write, naming what actually changed.

    A batch that skipped an unfetchable item has genuinely applied part of its
    change, so reporting a bare success (or a bare error) would misstate what
    happened to the mailbox. `success` is False when nothing at all was applied.

    Every entry in `failed` carries a stable `error_code` (see
    `utils.classify_item_error`); the distinct codes are also lifted to
    `failed_codes` so a caller can branch on the batch outcome without walking
    the list.
    """
    result: dict = {
        "success": bool(updated),
        "message": f"{action} {len(updated)} of {requested} email(s).",
        "updated_count": len(updated),
    }
    if failed:
        codes = list(dict.fromkeys(f.get("error_code", "") for f in failed if f.get("error_code")))
        result["failed_count"] = len(failed)
        result["failed"] = failed
        if codes:
            result["failed_codes"] = codes
        result["message"] += (
            f" {len(failed)} skipped (see 'failed' for a per-item "
            f"'error_code')."
        )
    return result


def _build_recipient_list(emails: str) -> list[dict]:
    """Build a list of Mailbox dicts from a comma-separated email string.

    NOTE: OWA rejects __type annotations on recipient Mailbox dicts for
    CreateItem (Message). Use plain dicts without __type.
    """
    recipients = []
    for addr in emails.split(","):
        addr = addr.strip()
        if addr:
            recipients.append(
                {
                    "Name": addr,
                    "EmailAddress": addr,
                    "RoutingType": "SMTP",
                }
            )
    return recipients


_AQS_LITE_KEYWORDS = {"subject", "from", "category", "isread", "hasattachment"}


def _parse_aqs_lite(query: str) -> tuple[list[str], dict[str, list[str]]]:
    """Split an AQS query into free-text terms and a subset of recognized
    keyword:value filters, for the client-side fallback in search_emails.
    """
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()

    terms: list[str] = []
    filters: dict[str, list[str]] = {}
    for token in tokens:
        match = re.match(r"^(\w+):(.+)$", token)
        if match and match.group(1).lower() in _AQS_LITE_KEYWORDS:
            filters.setdefault(match.group(1).lower(), []).append(match.group(2))
        else:
            terms.append(token)
    return terms, filters


def _local_search_matches(item: dict, terms: list[str], filters: dict[str, list[str]]) -> bool:
    """Best-effort match against fields already present in a FindItem/Default
    result, mirroring the keywords _parse_aqs_lite recognizes.
    """
    subject = (item.get("Subject") or "").lower()
    preview = (item.get("Preview") or "").lower()
    from_data = item.get("From", {}).get("Mailbox") or item.get("Sender", {}).get("Mailbox") or {}
    from_name = (from_data.get("Name") or "").lower()
    from_email = (from_data.get("EmailAddress") or "").lower()
    categories = [c.lower() for c in item.get("Categories", [])]

    for val in filters.get("subject", []):
        if val.lower() not in subject:
            return False
    for val in filters.get("from", []):
        v = val.lower()
        if v not in from_name and v not in from_email:
            return False
    for val in filters.get("category", []):
        if val.lower() not in categories:
            return False
    for val in filters.get("isread", []):
        if item.get("IsRead", False) != (val.lower() in ("true", "1", "yes")):
            return False
    for val in filters.get("hasattachment", []):
        if item.get("HasAttachments", False) != (val.lower() in ("true", "1", "yes")):
            return False

    haystack = f"{subject} {preview} {from_name} {from_email}"
    return all(term.lower() in haystack for term in terms)


def _list_all_folder_ids(client: OWAClient, max_folders: int = 50) -> list[str]:
    """Enumerate mail folder IDs under msgfolderroot via FindFolder/Deep.

    search_emails(search_all_folders=True) needs this instead of FindItem's
    own Traversal:"Deep": on at least one tenant, FindItem rejects Deep
    outright ("Invalid argument used to call method FindItem") regardless of
    folder or QueryString, while FindFolder/Deep - a different operation -
    works fine (see get_folders' recursive=True).
    """
    payload = {
        "__type": "FindFolderJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "FindFolderRequest:#Exchange",
            "FolderShape": {
                "__type": "FolderResponseShape:#Exchange",
                "BaseShape": "IdOnly",
            },
            "ParentFolderIds": [OWAClient.folder_id_dict("msgfolderroot")],
            "Traversal": "Deep",
            "Paging": {
                "__type": "IndexedPageView:#Exchange",
                "BasePoint": "Beginning",
                "Offset": 0,
                "MaxEntriesReturned": max_folders,
            },
        },
    }
    data = client.request("FindFolder", payload)

    ids = ["msgfolderroot"]
    for msg in client.extract_items(data):
        if "RootFolder" in msg and "Folders" in msg["RootFolder"]:
            for f in msg["RootFolder"]["Folders"]:
                fid = f.get("FolderId", {}).get("Id")
                if fid:
                    ids.append(fid)
    return ids[:max_folders]


def _search_folder_aqs(client: OWAClient, parent_folder_id: dict, query: str, limit: int) -> list[dict]:
    """One server-side AQS QueryString search against a single folder (Shallow).

    Returns [] on empty results or on any failure - including transport-level
    exceptions, which Traversal:"Deep" triggers outright on some tenants - so
    the caller can fall back to _local_search_fallback either way.
    """
    payload = {
        "__type": "FindItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "Exchange2013",
        },
        "Body": {
            "__type": "FindItemRequest:#Exchange",
            "ItemShape": {
                "__type": "ItemResponseShape:#Exchange",
                "BaseShape": "Default",
                "AdditionalProperties": [
                    {"__type": "PropertyUri:#Exchange", "FieldURI": "item:ParentFolderId"},
                ],
            },
            "ParentFolderIds": [parent_folder_id],
            "Traversal": "Shallow",
            "QueryString": {"__type": "QueryStringType:#Exchange", "Value": query},
            "Paging": {
                "__type": "IndexedPageView:#Exchange",
                "BasePoint": "Beginning",
                "Offset": 0,
                "MaxEntriesReturned": limit,
            },
        },
    }
    try:
        data = client.request("FindItem", payload)
    except SessionExpiredError:
        raise
    except Exception:
        return []

    for msg in client.extract_items(data):
        if msg.get("ResponseClass") == "Error":
            return []
        if "RootFolder" in msg:
            return msg["RootFolder"].get("Items", [])
    return []


def _local_search_fallback(
    client: OWAClient,
    parent_folder_ids: list[dict],
    traversal: str,
    query: str,
    limit: int,
    max_scan: int = 1000,
) -> list[dict]:
    """Page through items structurally (no QueryString) and filter client-side.

    Used when the server accepts QueryString but silently returns zero
    results for it - observed on at least one tenant's OWA backend, where
    FindItem's content-index search never actually runs.
    """
    terms, filters = _parse_aqs_lite(query)
    matches: list[dict] = []
    offset = 0
    page_size = 200

    while offset < max_scan and len(matches) < limit:
        payload = {
            "__type": "FindItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "FindItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "Default",
                    "AdditionalProperties": [
                        {"__type": "PropertyUri:#Exchange", "FieldURI": "item:ParentFolderId"},
                    ],
                },
                "ParentFolderIds": parent_folder_ids,
                "Traversal": traversal,
                "Paging": {
                    "__type": "IndexedPageView:#Exchange",
                    "BasePoint": "Beginning",
                    "Offset": offset,
                    "MaxEntriesReturned": page_size,
                },
            },
        }

        data = client.request("FindItem", payload)

        page_items: list[dict] = []
        includes_last = True
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                raise RuntimeError(msg.get("MessageText", "Search failed."))
            if "RootFolder" in msg:
                root = msg["RootFolder"]
                page_items = root.get("Items", [])
                includes_last = root.get("IncludesLastItemInRange", True)
                break

        if not page_items:
            break

        for item in page_items:
            if _local_search_matches(item, terms, filters):
                matches.append(item)
                if len(matches) >= limit:
                    break

        if includes_last:
            break
        offset += page_size

    return matches


# ------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------


@mcp.tool()
def get_emails(
    folder: str = "Inbox",
    limit: int = 10,
    offset: int = 0,
    include_body: bool = False,
    unread_only: bool = False,
    ids_only: bool = False,
    ctx: Context = None,
) -> str:
    """Get emails from a mailbox folder, grouped by conversation/thread.

    Each result row is one conversation (subject, participants, message
    count, unread count, last delivery time) rather than one row per
    message. Pass the returned item_id to get_email/include_body to read
    the latest message in a specific thread.

    Args:
        folder: Folder name (Inbox, Sent, Drafts, Deleted, Junk, or custom name).
        limit: Maximum number of conversations to return (default 10, max 50).
        offset: Number of conversations to skip for pagination.
        include_body: If True, fetch the latest message's full body for each
            conversation (slower).
        unread_only: If True, only return conversations with unread messages.
        ids_only: If True, return only conversation/item IDs and dates
            (compact, for bulk ops). Max limit raised to 500 in this mode.

    Each result includes flag_status ("NotFlagged"/"Flagged"/"Complete").
    At the default FindConversation shape this value is unverified against
    this tenant -- pass include_body=True for a value confirmed via GetItem.
    """
    try:
        client = _get_client(ctx)

        # Clamp limit (higher cap for ids_only)
        max_limit = 500 if ids_only else 50
        if limit > max_limit:
            limit = max_limit

        # Resolve folder name to ID
        folder_id = client.get_folder_id(folder)
        if not folder_id:
            return json.dumps({"error": f"Folder '{folder}' not found."})

        # FindConversation's server-side paging has been observed to not
        # always respect MaxEntriesReturned, so we over-fetch and clamp
        # offset/limit/unread_only client-side instead of trusting it.
        fetch_count = min(max(limit * 4, 50), 200)

        find_body = {
            "__type": "FindConversationRequest:#Exchange",
            "ParentFolderId": {
                "__type": "TargetFolderId:#Exchange",
                "BaseFolderId": OWAClient.folder_id_dict(folder_id),
            },
            "ConversationShape": {
                "__type": "ConversationResponseShape:#Exchange",
                "BaseShape": "IdOnly",
            },
            # Required by the modern Outlook backend: without it,
            # FindConversation rejects the request as "no query string,
            # traversal not allowed" even though this is a plain listing.
            "ShapeName": "ReactConversationListView",
            "ViewFilter": "All",
            "Paging": {
                "__type": "IndexedPageView:#Exchange",
                "BasePoint": "Beginning",
                "Offset": 0,
                "MaxEntriesReturned": fetch_count,
            },
        }

        payload = {
            "__type": "FindConversationJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": find_body,
        }

        data = client.request("FindConversation", payload)

        # FindConversation's response envelope is {"Body": {"Conversations":
        # [...]}}, not the classic {"Body": {"ResponseMessages": {"Items":
        # [...]}}} - extract_items() doesn't apply here.
        conversations = (data.get("Body") or {}).get("Conversations") or []

        if unread_only:
            conversations = [c for c in conversations if c.get("UnreadCount", 0) > 0]

        conversations = conversations[offset:offset + limit]

        if not conversations:
            return json.dumps(
                {"item_ids": [], "count": 0} if ids_only
                else {"emails": [], "count": 0}
            )

        if ids_only:
            result = []
            for conv in conversations:
                item_ids = conv.get("ItemIds") or conv.get("GlobalItemIds") or []
                result.append({
                    "conversation_id": conv.get("ConversationId", {}).get("Id", ""),
                    "item_id": item_ids[-1].get("Id", "") if item_ids else "",
                    "date": conv.get("LastDeliveryTime", ""),
                    "subject": conv.get("ConversationTopic", ""),
                })
            return json.dumps({"item_ids": result, "count": len(result)})

        emails = []
        for conv in conversations:
            email = _extract_conversation_summary(conv)

            if include_body and email["item_id"]:
                details, detail_error = _try_get_item_details(client, email["item_id"])
                if details is None:
                    # One unfetchable message must not cost the caller the whole
                    # page (see _try_get_item_details) — degrade just this row.
                    email["body"] = ""
                    email["body_error"] = detail_error
                else:
                    email["from"] = details["from"]
                    email["from_name"] = details["from_name"]
                    email["to"] = details["to"]
                    email["cc"] = details["cc"]
                    email["body"] = details["body"]
                    email["has_links"] = details.get("has_links", False)
                    email["flag_status"] = details["flag_status"]

            emails.append(email)

        return json.dumps({"emails": emails, "count": len(emails)})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to get emails: {e}"})


@mcp.tool()
def search_emails(
    query: str,
    folder: str = "Inbox",
    limit: int = 25,
    search_all_folders: bool = False,
    ctx: Context = None,
) -> str:
    """Full-text search for emails using Exchange's indexed AQS query syntax.

    Unlike get_emails (which lists/filters by folder, read state, etc.), this
    searches message content: a bare phrase matches subject/body/participants
    on the server's content index. Refine with AQS keyword:value pairs -
    subject:, body:, from:, to:, cc:, bcc:, participants:, category:,
    hasattachment:true/false, isread:true/false, importance:high, sent:/
    received: (dates, e.g. received:>2026-01-01), size:>5000. Quote a phrase
    for an exact match (subject:"project plan"); bare words are prefix/
    substring matches. Results are individual messages, not threads - pass
    an item_id to get_email for the full body.

    Some OWA backends accept the search but their content index never
    actually runs it (zero results with no error), and some combinations
    (e.g. search_all_folders) can fail outright on the same backends. Either
    way, this tool transparently falls back to a client-side scan of the
    target folder(s), matching a reduced subset of the same syntax (bare
    terms, subject:, from:, category:, isread:, hasattachment:) against each
    message's subject/preview/sender/categories - slower, and no real body
    search, but still returns something useful.

    Args:
        query: AQS query string, e.g. "budget report", 'from:alice subject:"Q3 plan"'.
        folder: Folder to search (Inbox, Sent, Drafts, Deleted, Junk, or custom
            name). Ignored if search_all_folders is True.
        limit: Maximum number of matching messages to return (default 25, max 100).
        search_all_folders: If True, search every mail folder in the mailbox
            instead of just `folder`.
    """
    try:
        client = _get_client(ctx)

        max_limit = 100
        if limit > max_limit:
            limit = max_limit

        if search_all_folders:
            folder_ids = _list_all_folder_ids(client)
            # Many folders each potentially needing a full local scan is
            # expensive - cap each folder's fallback scan depth accordingly.
            fallback_max_scan = 200
        else:
            folder_id = client.get_folder_id(folder)
            if not folder_id:
                return json.dumps({"error": f"Folder '{folder}' not found."})
            folder_ids = [folder_id]
            fallback_max_scan = 1000

        found_items: list[dict] = []
        used_fallback = False

        for fid in folder_ids:
            remaining = limit - len(found_items)
            if remaining <= 0:
                break
            parent_folder_id = OWAClient.folder_id_dict(fid)

            items = _search_folder_aqs(client, parent_folder_id, query, remaining)
            if not items:
                items = _local_search_fallback(
                    client, [parent_folder_id], "Shallow", query, remaining,
                    max_scan=fallback_max_scan,
                )
                if items:
                    used_fallback = True
            found_items.extend(items)

        results = []
        for item in found_items[:limit]:
            from_data = item.get("From", {}).get("Mailbox", {})
            if not from_data:
                from_data = item.get("Sender", {}).get("Mailbox", {})

            results.append(
                {
                    "item_id": item.get("ItemId", {}).get("Id", ""),
                    "folder_id": item.get("ParentFolderId", {}).get("Id", ""),
                    "subject": item.get("Subject") or "(No subject)",
                    "from": from_data.get("EmailAddress", ""),
                    "from_name": from_data.get("Name", ""),
                    "date": item.get(
                        "DateTimeSent",
                        item.get("DateTimeReceived", item.get("DateTimeCreated", "")),
                    ),
                    "is_read": item.get("IsRead", False),
                    "has_attachments": item.get("HasAttachments", False),
                    "importance": item.get("Importance", "Normal"),
                    "preview": item.get("Preview", ""),
                    "categories": item.get("Categories", []),
                }
            )

        return json.dumps({
            "emails": results,
            "count": len(results),
            "used_local_fallback": used_fallback,
        })

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to search emails: {e}"})


@mcp.tool()
def get_email(item_id: str, ctx: Context = None) -> str:
    """Get a single email with full body and details.

    Args:
        item_id: The Exchange ItemId of the email to retrieve.
    """
    try:
        client = _get_client(ctx)
        result = _get_item_details(client, item_id)
        return json.dumps(result)
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        # Say plainly which kind of failure this is, and give it a stable code:
        # a caller must not have to decide "retry vs. re-list vs. give up" by
        # substring-matching a .NET exception name out of an HTTP 500 body.
        code = classify_item_error(str(e))
        error = {"error": f"Failed to get email: {e}", "error_code": code}
        if code == ITEM_NOT_SERIALIZABLE:
            # The item is fine and so is the session: OWA faults while
            # *serialising its own response* to a full-property read. Narrower
            # reads of the same item still work, so point at them rather than
            # calling this unfixable (confirmed live 2026-09-11 on a
            # MeetingRequestMessage: AllProperties 500s, get_email_links'
            # IdOnly + named-property shape returns the item intact).
            error["hint"] = (
                "OWA faults while serialising its response to a full-property "
                "read of this item (server-side SerializationException); the "
                "item_id and session are fine, and writes to it work. Seen on "
                "MeetingRequestMessage items. Narrow reads of the same item do "
                "succeed: use get_emails (without include_body) for summary "
                "fields, get_email_links for subject + links, and the category "
                "tools, set_email_flag, mark_email_read and move_email all "
                "work on it normally."
            )
        return json.dumps(error)


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    importance: str = "Normal",
    is_html: bool = False,
    ctx: Context = None,
) -> str:
    """Send a new email.

    Args:
        to: Comma-separated list of recipient email addresses.
        subject: Email subject line.
        body: Email body text.
        cc: Comma-separated CC recipients (optional).
        bcc: Comma-separated BCC recipients (optional).
        importance: Email importance: Low, Normal, or High (default Normal).
        is_html: If True, body is treated as HTML. Otherwise plain text.
    """
    try:
        client = _get_client(ctx)

        to_recipients = _build_recipient_list(to)
        if not to_recipients:
            return json.dumps({"error": "At least one recipient is required."})

        message = {
            "__type": "Message:#Exchange",
            "Subject": subject,
            "Body": {
                "__type": "BodyContentType:#Exchange",
                "BodyType": "HTML" if is_html else "Text",
                "Value": body,
            },
            "Importance": importance,
            "ToRecipients": to_recipients,
        }

        if cc:
            message["CcRecipients"] = _build_recipient_list(cc)
        if bcc:
            message["BccRecipients"] = _build_recipient_list(bcc)

        payload = {
            "__type": "CreateItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "CreateItemRequest:#Exchange",
                "Items": [message],
                "MessageDisposition": "SendAndSaveCopy",
            },
        }

        data = client.request("CreateItem", payload)

        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Success":
                return json.dumps({"success": True, "message": "Email sent."})
            elif msg.get("ResponseClass") == "Error":
                return json.dumps(
                    {"error": msg.get("MessageText", "Failed to send email.")}
                )

        return json.dumps({"success": True, "message": "Email sent."})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to send email: {e}"})


@mcp.tool()
def reply_email(
    item_id: str,
    body: str,
    reply_all: bool = False,
    ctx: Context = None,
) -> str:
    """Reply to an email.

    Args:
        item_id: The Exchange ItemId of the email to reply to.
        body: Reply body text.
        reply_all: If True, reply to all recipients. Otherwise reply to sender only.
    """
    try:
        client = _get_client(ctx)

        change_key = _get_change_key(client, item_id)
        if not change_key:
            return json.dumps({"error": "Could not resolve item ChangeKey."})

        item_type = "ReplyAllToItem:#Exchange" if reply_all else "ReplyToItem:#Exchange"

        reply_item = {
            "__type": item_type,
            "ReferenceItemId": {
                "__type": "ItemId:#Exchange",
                "Id": item_id,
                "ChangeKey": change_key,
            },
            "NewBodyContent": {
                "__type": "BodyContentType:#Exchange",
                "BodyType": "Text",
                "Value": body,
            },
        }

        payload = {
            "__type": "CreateItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "CreateItemRequest:#Exchange",
                "Items": [reply_item],
                "MessageDisposition": "SendAndSaveCopy",
            },
        }

        data = client.request("CreateItem", payload)

        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                return json.dumps(
                    {"error": msg.get("MessageText", "Failed to send reply.")}
                )

        return json.dumps({"success": True, "message": "Reply sent."})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to reply: {e}"})


@mcp.tool()
def forward_email(
    item_id: str,
    to: str,
    body: str = "",
    ctx: Context = None,
) -> str:
    """Forward an email to other recipients.

    Args:
        item_id: The Exchange ItemId of the email to forward.
        to: Comma-separated list of recipient email addresses.
        body: Optional message to include above the forwarded content.
    """
    try:
        client = _get_client(ctx)

        to_recipients = _build_recipient_list(to)
        if not to_recipients:
            return json.dumps({"error": "At least one recipient is required."})

        change_key = _get_change_key(client, item_id)
        if not change_key:
            return json.dumps({"error": "Could not resolve item ChangeKey."})

        forward_item = {
            "__type": "ForwardItem:#Exchange",
            "ReferenceItemId": {
                "__type": "ItemId:#Exchange",
                "Id": item_id,
                "ChangeKey": change_key,
            },
            "ToRecipients": to_recipients,
        }

        if body:
            forward_item["NewBodyContent"] = {
                "__type": "BodyContentType:#Exchange",
                "BodyType": "Text",
                "Value": body,
            }

        payload = {
            "__type": "CreateItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "CreateItemRequest:#Exchange",
                "Items": [forward_item],
                "MessageDisposition": "SendAndSaveCopy",
            },
        }

        data = client.request("CreateItem", payload)

        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                return json.dumps(
                    {"error": msg.get("MessageText", "Failed to forward.")}
                )

        return json.dumps({"success": True, "message": "Email forwarded."})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to forward email: {e}"})


@mcp.tool()
def mark_email_read(
    item_ids: list[str],
    is_read: bool = True,
    ctx: Context = None,
) -> str:
    """Mark one or more emails as read or unread.

    Args:
        item_ids: List of Exchange ItemIds to update.
        is_read: True to mark as read, False to mark as unread (default True).
    """
    try:
        client = _get_client(ctx)

        changes = []
        for iid in item_ids:
            change_key = _get_change_key(client, iid)
            item_id_dict = {"__type": "ItemId:#Exchange", "Id": iid}
            if change_key:
                item_id_dict["ChangeKey"] = change_key
            changes.append(
                {
                    "__type": "ItemChange:#Exchange",
                    "ItemId": item_id_dict,
                    "Updates": [
                        {
                            "__type": "SetItemField:#Exchange",
                            "Path": {
                                "__type": "PropertyUri:#Exchange",
                                "FieldURI": "IsRead",
                            },
                            "Item": {
                                "__type": "Message:#Exchange",
                                "IsRead": is_read,
                            },
                        }
                    ],
                }
            )

        payload = {
            "__type": "UpdateItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "UpdateItemRequest:#Exchange",
                "ItemChanges": changes,
                "ConflictResolution": "AutoResolve",
                "MessageDisposition": "SaveOnly",
            },
        }

        data = client.request("UpdateItem", payload)

        errors = []
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                errors.append(msg.get("MessageText", "Unknown error"))

        if errors:
            return json.dumps({"error": "; ".join(errors)})

        status = "read" if is_read else "unread"
        return json.dumps(
            {
                "success": True,
                "message": f"Marked {len(item_ids)} email(s) as {status}.",
            }
        )

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to update emails: {e}"})


_VALID_FLAG_STATUSES = {"NotFlagged", "Flagged", "Complete"}

def _build_flag_update(flag_status: str) -> dict:
    """Build the SetItemField update that sets an item's follow-up flag.

    The exact wire encoding here is load-bearing and was found by elimination
    against a live mailbox (2026-09-10). Everything else this backend accepts
    for other fields is rejected for the flag:

      * `FieldURI: "message:Flag"` with either `Flag:#Exchange` or
        `FlagType:#Exchange` -> "Invalid argument used to call method UpdateItem"
      * `PidLidFlagStatus` (PSETID_Common 0x8530) as an ExtendedFieldURI, in
        every spelling tried (`PathToExtendedFieldType`/`ExtendedPropertyUri`
        x `DistinguishedPropertySetId`/`PropertySetId` GUID, plus a
        `PropertyTag` for PidTagFollowupIcon) -> ErrorCode 500, or
        "the combination of extended property attributes is not valid"

    What works is the `item:`-namespaced field URI paired with the `FlagType`
    complex type. Change either half and the call starts failing again.
    """
    return {
        "__type": "SetItemField:#Exchange",
        "Path": {"__type": "PropertyUri:#Exchange", "FieldURI": "item:Flag"},
        "Item": {
            "__type": "Message:#Exchange",
            "Flag": {"__type": "FlagType:#Exchange", "FlagStatus": flag_status},
        },
    }


@mcp.tool()
def set_email_flag(
    item_ids: list[str],
    flag_status: str,
    ctx: Context = None,
) -> str:
    """Set the follow-up flag on one or more emails.

    Args:
        item_ids: List of Exchange ItemIds to update.
        flag_status: One of "NotFlagged", "Flagged", "Complete".

    Verified live 2026-09-10: writing and reading back all three states
    round-trips correctly. The wire encoding is fussy on this backend --
    see _build_flag_update() for what was rejected and why.
    """
    if flag_status not in _VALID_FLAG_STATUSES:
        return json.dumps(
            {
                "error": f"Invalid flag_status: {flag_status}. Must be one of "
                f"{sorted(_VALID_FLAG_STATUSES)}."
            }
        )
    try:
        client = _get_client(ctx)

        changes = []
        for iid in item_ids:
            change_key = _get_change_key(client, iid)
            item_id_dict = {"__type": "ItemId:#Exchange", "Id": iid}
            if change_key:
                item_id_dict["ChangeKey"] = change_key
            changes.append(
                {
                    "__type": "ItemChange:#Exchange",
                    "ItemId": item_id_dict,
                    "Updates": [_build_flag_update(flag_status)],
                }
            )

        payload = {
            "__type": "UpdateItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "UpdateItemRequest:#Exchange",
                "ItemChanges": changes,
                "ConflictResolution": "AutoResolve",
                "MessageDisposition": "SaveOnly",
            },
        }

        data = client.request("UpdateItem", payload)

        errors = []
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                errors.append(msg.get("MessageText", "Unknown error"))

        if errors:
            return json.dumps({"error": "; ".join(errors)})

        return json.dumps(
            {
                "success": True,
                "message": f"Set flag_status={flag_status} on {len(item_ids)} email(s).",
            }
        )

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to set email flag: {e}"})


@mcp.tool()
def move_email(
    item_ids: list[str],
    target_folder: str,
    ctx: Context = None,
) -> str:
    """Move one or more emails to a different folder.

    Args:
        item_ids: List of Exchange ItemIds to move.
        target_folder: Destination folder name (e.g. Inbox, Sent, Deleted, or custom).
    """
    try:
        client = _get_client(ctx)

        folder_id = client.get_folder_id(target_folder)
        if not folder_id:
            return json.dumps({"error": f"Folder '{target_folder}' not found."})

        items = [
            {"__type": "ItemId:#Exchange", "Id": iid} for iid in item_ids
        ]

        payload = {
            "__type": "MoveItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "MoveItemRequest:#Exchange",
                "ItemIds": items,
                "ToFolderId": {
                    "__type": "TargetFolderId:#Exchange",
                    "BaseFolderId": OWAClient.folder_id_dict(folder_id),
                },
            },
        }

        data = client.request("MoveItem", payload)

        errors = []
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                errors.append(msg.get("MessageText", "Unknown error"))

        if errors:
            return json.dumps({"error": "; ".join(errors)})

        return json.dumps(
            {
                "success": True,
                "message": f"Moved {len(item_ids)} email(s) to '{target_folder}'.",
            }
        )

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to move emails: {e}"})


@mcp.tool()
def delete_email(
    item_ids: list[str],
    permanent: bool = False,
    ctx: Context = None,
) -> str:
    """Delete one or more emails.

    Args:
        item_ids: List of Exchange ItemIds to delete.
        permanent: If True, permanently delete (HardDelete). Otherwise move to Deleted Items.
    """
    try:
        client = _get_client(ctx)

        items = [
            {"__type": "ItemId:#Exchange", "Id": iid} for iid in item_ids
        ]

        payload = {
            "__type": "DeleteItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "DeleteItemRequest:#Exchange",
                "ItemIds": items,
                "DeleteType": "HardDelete" if permanent else "MoveToDeletedItems",
            },
        }

        data = client.request("DeleteItem", payload)

        errors = []
        for msg in client.extract_items(data):
            if msg.get("ResponseClass") == "Error":
                errors.append(msg.get("MessageText", "Unknown error"))

        if errors:
            return json.dumps({"error": "; ".join(errors)})

        action = "permanently deleted" if permanent else "moved to Deleted Items"
        return json.dumps(
            {
                "success": True,
                "message": f"{len(item_ids)} email(s) {action}.",
            }
        )

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to delete emails: {e}"})


@mcp.tool()
def download_attachments(
    item_id: str,
    target_folder: str = "/tmp/attachments",
    ctx: Context = None,
) -> str:
    """Download all file attachments from an email to disk.

    Args:
        item_id: The Exchange ItemId of the email to download attachments from.
        target_folder: Local directory to save files (default /tmp/attachments).
    """
    import os

    try:
        client = _get_client(ctx)

        # Get email details to find attachment IDs
        details = _get_item_details(client, item_id)
        attachments = details.get("attachments", [])

        if not attachments:
            return json.dumps({"success": True, "downloaded": [], "count": 0,
                               "message": "No attachments found."})

        # Filter to non-inline file attachments with IDs
        file_attachments = [
            a for a in attachments
            if a.get("attachment_id") and not a.get("is_inline", False)
        ]

        if not file_attachments:
            return json.dumps({"success": True, "downloaded": [], "count": 0,
                               "message": "No downloadable file attachments."})

        os.makedirs(target_folder, exist_ok=True)

        downloaded = []
        errors = []
        used_names: set[str] = set()

        for att in file_attachments:
            try:
                content, filename, content_type = client.download_file(
                    att["attachment_id"]
                )

                # Sanitize filename
                filename = os.path.basename(filename)
                if not filename:
                    filename = att.get("name", "attachment") or "attachment"

                # Handle collisions
                base_name = filename
                name_part, _, ext_part = base_name.rpartition(".")
                if not name_part:
                    name_part = base_name
                    ext_part = ""

                counter = 1
                while filename.lower() in used_names:
                    if ext_part:
                        filename = f"{name_part}_{counter}.{ext_part}"
                    else:
                        filename = f"{name_part}_{counter}"
                    counter += 1

                used_names.add(filename.lower())

                filepath = os.path.join(target_folder, filename)
                with open(filepath, "wb") as f:
                    f.write(content)

                downloaded.append({
                    "name": filename,
                    "path": filepath,
                    "size": len(content),
                    "content_type": content_type,
                })
            except Exception as e:
                errors.append({
                    "name": att.get("name", "unknown"),
                    "error": str(e),
                })

        result = {
            "success": len(errors) == 0,
            "downloaded": downloaded,
            "count": len(downloaded),
        }
        if errors:
            result["errors"] = errors

        return json.dumps(result)

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to download attachments: {e}"})


@mcp.tool()
def get_email_links(
    item_id: str,
    ctx: Context = None,
) -> str:
    """Extract all hyperlinks from an email's HTML body.

    Args:
        item_id: The Exchange ItemId of the email to extract links from.
    """
    try:
        client = _get_client(ctx)

        # Fetch email with HTML body
        payload = {
            "__type": "GetItemJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "V2017_08_18",
            },
            "Body": {
                "__type": "GetItemRequest:#Exchange",
                "ItemShape": {
                    "__type": "ItemResponseShape:#Exchange",
                    "BaseShape": "IdOnly",
                    "BodyType": "HTML",
                    "AdditionalProperties": [
                        {
                            "__type": "PropertyUri:#Exchange",
                            "FieldURI": "Subject",
                        },
                        {
                            "__type": "PropertyUri:#Exchange",
                            "FieldURI": "Body",
                        },
                    ],
                },
                "ItemIds": [{"__type": "ItemId:#Exchange", "Id": item_id}],
            },
        }

        data = client.request("GetItem", payload)

        subject = ""
        links = []

        for msg in client.extract_items(data):
            if "Items" not in msg:
                continue
            for item in msg["Items"]:
                subject = item.get("Subject", "")
                body_val = item.get("Body", {}).get("Value", "")
                links = extract_links_from_html(body_val)
                break

        return json.dumps({
            "item_id": item_id,
            "subject": subject,
            "links": links,
            "count": len(links),
        })

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to extract links: {e}"})


def _set_email_categories(client: OWAClient, item_ids: list[str], categories: list[str]) -> None:
    """Overwrite the Categories field on each item via UpdateItem/SetItemField."""
    changes = []
    for iid in item_ids:
        change_key = _get_change_key(client, iid)
        item_id_dict = {"__type": "ItemId:#Exchange", "Id": iid}
        if change_key:
            item_id_dict["ChangeKey"] = change_key
        changes.append(
            {
                "__type": "ItemChange:#Exchange",
                "ItemId": item_id_dict,
                "Updates": [
                    {
                        "__type": "SetItemField:#Exchange",
                        "Path": {
                            "__type": "PropertyUri:#Exchange",
                            "FieldURI": "Categories",
                        },
                        "Item": {
                            "__type": "Message:#Exchange",
                            "Categories": categories,
                        },
                    }
                ],
            }
        )

    payload = {
        "__type": "UpdateItemJsonRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2017_08_18",
        },
        "Body": {
            "__type": "UpdateItemRequest:#Exchange",
            "ItemChanges": changes,
            "ConflictResolution": "AutoResolve",
            "MessageDisposition": "SaveOnly",
        },
    }

    data = client.request("UpdateItem", payload)
    for msg in client.extract_items(data):
        if msg.get("ResponseClass") == "Error":
            raise RuntimeError(msg.get("MessageText", "UpdateItem failed."))


@mcp.tool()
def assign_email_categories(
    item_ids: list[str],
    categories: list[str],
    ctx: Context = None,
) -> str:
    """Add one or more categories to emails, keeping any categories already present.

    Categories are just strings on the item (standard EWS behavior) - any
    name works, including ones not present in the mailbox's master category
    list (see the category_* tools). Assigning a brand-new name does not
    register it in the master list or give it a color.

    Works on mail-class items generally, not just plain Message items: verified
    live 2026-09-11 on a MeetingRequestMessage (a meeting invite in the Inbox),
    which previously failed. Nothing here is item-class-specific, so the
    neighbouring classes (MeetingResponseMessage, MeetingCancellation) take the
    same path - they just haven't each been exercised individually. For an item
    on the *calendar* rather than in a mail folder, use assign_event_categories
    instead: that one needs OWA's bespoke UpdateCalendarEvent action.

    Args:
        item_ids: List of Exchange ItemIds to tag.
        categories: Category names to add.

    Per-item failures never abort the batch: each is reported in `failed` with a
    stable `error_code` (`item_not_serializable`, `item_not_found`,
    `item_access_denied`, or `item_read_failed` for anything unrecognised), so
    callers can branch on the code instead of matching an HTTP 500 message.
    """
    try:
        client = _get_client(ctx)
        updated, failed = [], []
        for iid in item_ids:
            try:
                existing = _get_item_categories(client, iid)
                merged = list(dict.fromkeys(existing + categories))
                _set_email_categories(client, [iid], merged)
            except SessionExpiredError:
                raise
            except Exception as e:
                failed.append(item_error(iid, str(e)))
                continue
            updated.append(iid)
        return json.dumps(_bulk_result("Added categories to", updated, failed, len(item_ids)))
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to assign categories: {e}"})


@mcp.tool()
def remove_email_categories(
    item_ids: list[str],
    categories: list[str],
    ctx: Context = None,
) -> str:
    """Remove one or more categories from emails, keeping any others present.

    Works on mail-class items generally, meeting invites included - see
    assign_email_categories for the details and for the calendar-side
    equivalent.

    Args:
        item_ids: List of Exchange ItemIds to untag.
        categories: Category names to remove (case-insensitive match).

    Per-item failures never abort the batch: each is reported in `failed` with a
    stable `error_code` - see assign_email_categories.
    """
    try:
        client = _get_client(ctx)
        lowered = {c.lower() for c in categories}
        updated, failed = [], []
        for iid in item_ids:
            try:
                remaining = [c for c in _get_item_categories(client, iid)
                             if c.lower() not in lowered]
                _set_email_categories(client, [iid], remaining)
            except SessionExpiredError:
                raise
            except Exception as e:
                failed.append(item_error(iid, str(e)))
                continue
            updated.append(iid)
        return json.dumps(_bulk_result("Removed categories from", updated, failed, len(item_ids)))
    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to remove categories: {e}"})


@mcp.tool()
def find_emails_by_category(
    category: str,
    folder: str = "Inbox",
    limit: int = 10,
    ctx: Context = None,
) -> str:
    """Find email conversations tagged with a given category.

    Args:
        category: Category name to search for (case-insensitive match).
        folder: Folder name to search within (Inbox, Sent, Drafts, Deleted, or custom).
        limit: Maximum number of matching conversations to return (default 10, max 50).
    """
    try:
        client = _get_client(ctx)
        max_limit = 50
        if limit > max_limit:
            limit = max_limit

        folder_id = client.get_folder_id(folder)
        if not folder_id:
            return json.dumps({"error": f"Folder '{folder}' not found."})

        find_body = {
            "__type": "FindConversationRequest:#Exchange",
            "ParentFolderId": {
                "__type": "TargetFolderId:#Exchange",
                "BaseFolderId": OWAClient.folder_id_dict(folder_id),
            },
            "ConversationShape": {
                "__type": "ConversationResponseShape:#Exchange",
                "BaseShape": "IdOnly",
            },
            "ShapeName": "ReactConversationListView",
            "ViewFilter": "All",
            "Paging": {
                "__type": "IndexedPageView:#Exchange",
                "BasePoint": "Beginning",
                "Offset": 0,
                "MaxEntriesReturned": 200,
            },
        }

        payload = {
            "__type": "FindConversationJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": find_body,
        }

        data = client.request("FindConversation", payload)
        conversations = (data.get("Body") or {}).get("Conversations") or []

        category_lower = category.lower()
        matches = [
            c for c in conversations
            if any(cat.lower() == category_lower for cat in (c.get("Categories") or []))
        ]

        emails = [_extract_conversation_summary(c) for c in matches[:limit]]
        return json.dumps({"emails": emails, "count": len(emails)})

    except SessionExpiredError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": f"Failed to find emails by category: {e}"})
