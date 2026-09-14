"""Pure-logic tests for `OWAClient.folder_id_dict()` — the typed folder
reference every folder-scoped request is built from. No mailbox, no browser, no
EXCHANGE_OWA_URL (it's a `@staticmethod`, so no client instance either).

Why this one function deserves its own suite: it decides between
`DistinguishedFolderId` and `FolderId` for **every** folder-scoped payload in
the codebase — `get_emails`/`move_email`/`search_emails` (email.py),
`get_folders`/`create_folder`/`move_folder` (folders.py), the calendar and tasks
reads, and analytics/availability. `get_folder_id()` returns the *bare*
distinguished name ("inbox") rather than a resolved opaque ID on the classic
backend, so a wrong wrapper here is not a local mistake: it produces a payload
Exchange rejects (or, worse, silently resolves elsewhere) for whichever subset
of tools happens to hit that folder, and the symptom appears far from the cause.

Three behaviours are pinned:

1. **Every value in `DISTINGUISHED_FOLDERS` must resolve as distinguished.**
   That dict maps user-facing names (English + Russian) onto distinguished IDs
   and `_DISTINGUISHED_IDS` is derived from its *values* plus a hand-written
   extra set — so adding an alias for a folder that isn't in either place yields
   a `FolderId` wrapper around a bare name like "inbox", which is the one
   combination Exchange cannot make sense of.
2. **An opaque ID must never be mistaken for a distinguished one.** Real IDs are
   long base64 blobs containing "/" and "=" — the same shape that makes
   `tasks.py`'s `_looks_like_folder_id()` necessary.
3. **A user-facing *name* is not a distinguished ID.** "sent"/"deleted"/"junk"
   are keys of `DISTINGUISHED_FOLDERS`, not values, so they must go through
   `get_folder_id()` first; this function's contract starts at a resolved ID.
   `tasks.py`'s `_resolve_task_folder()` leans on exactly this line — it asks
   `folder_id_dict()` whether a bare string is distinguished rather than keeping
   a second copy of the list.

Run:
    python -m tests.unit.test_folder_id_dict
"""

import sys

from exchange_mcp.owa_client import (
    DISTINGUISHED_FOLDERS,
    _DISTINGUISHED_IDS,
    OWAClient,
)

DISTINGUISHED = "DistinguishedFolderId:#Exchange"
OPAQUE = "FolderId:#Exchange"

# A real-shaped opaque folder ID: long, base64, "/" and "=" inside it.
LIVE_OPAQUE_ID = (
    "AAMkADQ5ZmRlNTA2LWU5ZGEtNDk3Ni05MTM4LTBhYWFhYWFhYWFhYQAuAAAAAAB1"
    "d/pTdXNiTLuMLlYsvxKMAQBOTmJhY2thZ2UvZm9sZGVyL2lkAAAAAAEMAAA="
)

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def test_distinguished_id() -> None:
    check(
        "inbox -> DistinguishedFolderId",
        OWAClient.folder_id_dict("inbox"),
        {"__type": DISTINGUISHED, "Id": "inbox"},
    )


def test_opaque_id() -> None:
    check(
        "opaque base64 ID -> FolderId",
        OWAClient.folder_id_dict(LIVE_OPAQUE_ID),
        {"__type": OPAQUE, "Id": LIVE_OPAQUE_ID},
    )


def test_payload_shape() -> None:
    """Exactly two keys, and the ID is passed through untouched — the whole
    request is built around this dict, so an extra or renamed key is a wire bug."""
    for folder_id in ("inbox", LIVE_OPAQUE_ID):
        result = OWAClient.folder_id_dict(folder_id)
        check(f"{folder_id[:12]}... keys", sorted(result), ["Id", "__type"])
        check(f"{folder_id[:12]}... Id passed through", result["Id"], folder_id)


def test_every_distinguished_folder_value_is_distinguished() -> None:
    """The invariant that catches a new alias pointing at an unlisted ID."""
    for name, distinguished_id in sorted(DISTINGUISHED_FOLDERS.items()):
        # ascii() rather than !r: the Russian aliases would raise on a piped
        # stdout under a non-UTF-8 Windows codepage, turning a readable failure
        # report into a UnicodeEncodeError traceback.
        check(
            f"DISTINGUISHED_FOLDERS[{ascii(name)}] = {distinguished_id!r} -> DistinguishedFolderId",
            OWAClient.folder_id_dict(distinguished_id)["__type"],
            DISTINGUISHED,
        )


def test_every_known_distinguished_id_is_distinguished() -> None:
    """Same check from the other direction: the set is the authority, so nothing
    in it may fall through to the FolderId branch."""
    for distinguished_id in sorted(_DISTINGUISHED_IDS):
        check(
            f"{distinguished_id!r} -> DistinguishedFolderId",
            OWAClient.folder_id_dict(distinguished_id)["__type"],
            DISTINGUISHED,
        )


def test_ids_callers_pass_straight_through() -> None:
    """IDs no name maps onto, handed in directly by callers: `msgfolderroot` by
    email.py's mailbox-wide `FindFolder`, `tasks` by every tasks.py read (and by
    `create_folder(parent_folder_id="tasks")` when making a To Do list)."""
    for distinguished_id in ("msgfolderroot", "root", "tasks", "notes", "contacts",
                             "searchfolders", "publicfoldersroot", "favorites",
                             "deleteditems", "junkemail", "sentitems"):
        check(
            f"{distinguished_id!r} -> DistinguishedFolderId",
            OWAClient.folder_id_dict(distinguished_id)["__type"],
            DISTINGUISHED,
        )


def test_matching_is_case_insensitive() -> None:
    """The *type* decision must not depend on casing.

    Note the `Id` keeps the caller's casing — callers pass already-lowercased
    values (`get_folder_id()` resolves to lowercase, `tasks.py` lowercases before
    asking), so this only ever matters for the type check itself.
    """
    for variant in ("Inbox", "INBOX", "InBoX", "MsgFolderRoot", "DeletedItems"):
        check(f"{variant!r} -> DistinguishedFolderId",
              OWAClient.folder_id_dict(variant)["__type"], DISTINGUISHED)


def test_user_facing_names_are_not_distinguished_ids() -> None:
    """A name is not an ID: these are keys of DISTINGUISHED_FOLDERS whose value
    differs from the key, so they must be resolved by get_folder_id() first.

    This is the boundary `tasks.py`'s `_resolve_task_folder()` relies on — it
    only accepts a bare string as a folder after every name lookup has missed
    *and* this function calls it distinguished.
    """
    for name in ("sent", "deleted", "junk", "отправленные", "удаленные",
                 "нежелательная почта", "исходящие"):
        check(f"{ascii(name)} is a name, not an ID -> FolderId",
              OWAClient.folder_id_dict(name)["__type"], OPAQUE)


def test_custom_folder_names_are_not_distinguished() -> None:
    """A user's own folder — however plausible the name — is never distinguished."""
    for name in ("Projects", "Inbox/Triage", "Archive 2026", "todo", "task", "inboxes"):
        check(f"{name!r} -> FolderId",
              OWAClient.folder_id_dict(name)["__type"], OPAQUE)


def main() -> bool:
    for test in (
        test_distinguished_id,
        test_opaque_id,
        test_payload_shape,
        test_every_distinguished_folder_value_is_distinguished,
        test_every_known_distinguished_id_is_distinguished,
        test_ids_callers_pass_straight_through,
        test_matching_is_case_insensitive,
        test_user_facing_names_are_not_distinguished_ids,
        test_custom_folder_names_are_not_distinguished,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_folder_id_dict: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
