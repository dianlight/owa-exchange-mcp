"""Pure-logic tests for `OWAClient.resolve_folder()` — how a caller-supplied
folder name / path / id becomes an Exchange folder id. No mailbox, no browser:
a fake transport answers FindFolder/GetFolder from an in-memory folder tree and
records every request, so the *number* of requests is assertable too.

Why this needs its own suite: `resolve_folder()` is the single gate in front of
`move_email`'s `target_folder` and `get_emails`/`search_emails`/
`find_emails_by_category`'s `folder`, and every way it can fail looks identical
from outside — `{"error": "Folder 'X' not found."}` on a folder that plainly
exists. Two such failures were reported live from an unattended run and are
pinned here:

1. **An opaque folder id, passed exactly as `get_folders` returned it, resolved
   to nothing.** Those ids are base64 and routinely contain "/", and the
   resolver split on "/" to walk a path *before* considering that the string
   might be an id — so a valid id was chopped into nonsense segments. The id
   form is the one a caller can't get wrong, which makes this the worst
   possible thing to break: `test_opaque_id_*`.
2. **A folder one level under the Inbox was invisible by name** ("Quarantena",
   confirmed live to be an Inbox child even though a recursive listing of
   `msgfolderroot` shows it flattened in among the top-level folders). The name
   lookup only ever searched direct children of `msgfolderroot`:
   `test_inbox_child_by_name`.

Two more failure modes are pinned because this backend invites them:

3. **A single FindFolder request is not the folder list.** MaxEntriesReturned is
   not honoured reliably, so resolution pages and stops only on an empty page —
   the same rule email.py's conversation paging follows. A mailbox with more
   folders than one page could not resolve anything past it:
   `test_name_found_on_second_page`.
4. **A name several folders share must not be guessed.** Silently filing mail
   into whichever "Prj-*" the server happened to list first is worse than
   failing, so that returns `folder_name_ambiguous` plus every candidate id:
   `test_ambiguous_deep_name`.

Run:
    python -m tests.unit.test_folder_resolution
"""

import base64
import sys

from exchange_mcp.owa_client import OWAClient, looks_like_folder_id

FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    if actual != expected:
        FAILURES.append(f"{label}: expected {expected!r}, got {actual!r}")


def fake_id(name: str) -> str:
    """A real-shaped opaque folder id: long, base64 charset, "/" and "=" inside.

    The "/" is the point — it's what made the live ids indistinguishable from
    "/"-delimited paths to the old resolver.
    """
    raw = base64.b64encode(name.encode()).decode()
    return ("AQMkADljNzNkMTcx" + raw + "/U6x0UA6hmSFxQEAqDv117Iz9k").ljust(84, "A") + "=="


# The tree the fake transport serves. Keys are parent folder ids (a
# distinguished name, or a folder's display name standing in for its id here);
# values are child display names.
#
# Shapes chosen to mirror the reporting mailbox: ~250 top-level folders (more
# than one FindFolder page), user folders nested under the Inbox, and several
# folders sharing a short "Prj-*" name at different depths.
TOP_LEVEL = (
    [f"Filler-{i:03d}" for i in range(1, 250)]
    + ["Deep Parent", "Other Parent", "Prj-Alpha"]
)
TREE = {
    "msgfolderroot": TOP_LEVEL,
    "inbox": ["Quarantena", "Save The Date"],
    "Deep Parent": ["Nested Unique", "Prj-Alpha", "Prj-Beta"],
    "Other Parent": ["Prj-Beta"],
}

# Ids are per *folder*, not per name: two folders sharing a display name have
# different ids, which is the whole reason an id is the unambiguous form.
CHILD_IDS: dict[tuple[str, str], str] = {}
# id -> the TREE key whose children that folder has, so the fake can serve a
# FindFolder rooted at an id (which is how a "/"-path walk addresses segment 2+).
ID_TO_KEY: dict[str, str] = {}

for _parent, _names in TREE.items():
    for _name in _names:
        _fid = fake_id(f"{_parent}:{_name}")
        CHILD_IDS[(_parent, _name)] = _fid
        ID_TO_KEY[_fid] = _name
for _distinguished in ("inbox", "msgfolderroot"):
    ID_TO_KEY[fake_id(_distinguished)] = _distinguished


def child_id(parent: str, name: str) -> str:
    return CHILD_IDS[(parent, name)]


class FakeBrowser:
    def __init__(self, auth_mode: str = "canary"):
        self.auth_mode = auth_mode
        self.owa_url = "https://owa.example.com"
        self.profile_dir = "/tmp/profile"


class FakeClient(OWAClient):
    """OWAClient with `request()` served from TREE instead of a browser tab.

    `page_size` caps how many folders one response carries regardless of the
    MaxEntriesReturned asked for (this backend under-fills pages), and
    `ignore_offset=True` reproduces a backend that hands back page 1 forever.
    """

    def __init__(self, *, auth_mode: str = "canary", page_size: int = 200,
                 ignore_offset: bool = False):
        super().__init__(FakeBrowser(auth_mode))
        self.page_size = page_size
        self.ignore_offset = ignore_offset
        self.calls: list[tuple[str, str, int]] = []

    def _children(self, parent_id: str, traversal: str) -> list[tuple[str, str]]:
        """(parent_key, display_name) pairs, so each row gets its own folder id."""
        key = ID_TO_KEY.get(parent_id, parent_id)
        direct = [(key, name) for name in TREE.get(key, [])]
        if traversal != "Deep":
            return direct
        out: list[tuple[str, str]] = []
        queue = list(direct)
        while queue:
            parent, name = queue.pop(0)
            out.append((parent, name))
            queue.extend((name, grandchild) for grandchild in TREE.get(name, []))
        return out

    def request(self, action: str, payload: dict, *, timeout: int = 30) -> dict:
        body = payload["Body"]

        if action == "GetFolder":
            distinguished = body["FolderIds"][0]["Id"]
            self.calls.append((action, distinguished, 0))
            return _envelope([{"Folders": [{"FolderId": {"Id": fake_id(distinguished)}}]}])

        if action != "FindFolder":
            raise AssertionError(f"unexpected action {action}")

        parent_id = body["ParentFolderIds"][0]["Id"]
        traversal = body["Traversal"]
        offset = body["Paging"]["Offset"]
        self.calls.append((action, parent_id, offset))

        rows = self._children(parent_id, traversal)
        start = 0 if self.ignore_offset else offset
        page = rows[start:start + self.page_size]
        folders = [
            {
                "DisplayName": name,
                "FolderId": {"Id": child_id(parent, name)},
                "TotalCount": 300 if name == "Quarantena" else 0,
            }
            for parent, name in page
        ]
        return _envelope([{"RootFolder": {"Folders": folders}}])


def _envelope(items: list[dict]) -> dict:
    return {"Body": {"ResponseMessages": {"Items": items}}}


# ------------------------------------------------------------------
# looks_like_folder_id
# ------------------------------------------------------------------


def test_looks_like_folder_id() -> None:
    live_id = (
        "AQMkADljNzNkMTcxLWVmMGUtNDk1OS04OTI1LTdiNzNkYmYxYmQwYQAuAAAD+/2B6g5R"
        "/U6x0UA6hmSFxQEAqDv117Iz9kCZM0FGk8qdjwACEH3sxgAAAA=="
    )
    check("live folder id from get_folders", looks_like_folder_id(live_id), True)
    check("generated fake id", looks_like_folder_id(child_id("inbox", "Quarantena")), True)

    for name in ("Quarantena", "Inbox/Quarantena", "Prj-Alpha", "", "   "):
        check(f"{name!r} is not an id", looks_like_folder_id(name), False)

    # Length alone must not decide it: a long *name* has spaces/punctuation
    # outside the base64 charset, which is what keeps this test cheap and safe.
    long_name = "Archivio progetti molto vecchi da rivedere prima della fine dell'anno 2026 (bozza)"
    check("long display name is not an id", looks_like_folder_id(long_name), False)


# ------------------------------------------------------------------
# The two reported failures
# ------------------------------------------------------------------


def test_opaque_id_passes_through_untouched() -> None:
    """The regression: a valid id must resolve to itself, with no lookup at all.

    Zero requests is part of the contract, not an optimisation — a lookup is
    exactly what an id exists to avoid, and it's why an id is safe to use from
    an unattended task whatever the folder is called.
    """
    client = FakeClient()
    folder_id = child_id("inbox", "Quarantena")
    resolution = client.resolve_folder(folder_id)
    check("id resolves to itself", resolution.folder_id, folder_id)
    check("matched_by", resolution.matched_by, "folder_id")
    check("no error", resolution.error_code, None)
    check("no requests issued", client.calls, [])


def test_opaque_id_containing_slash_is_not_read_as_a_path() -> None:
    """The specific mechanism of the bug: "/" inside the id used to win."""
    client = FakeClient()
    folder_id = child_id("inbox", "Quarantena")
    check("id contains '/'", "/" in folder_id, True)
    check("still resolves as an id", client.resolve_folder(folder_id).matched_by, "folder_id")


def test_inbox_child_by_name() -> None:
    """"Quarantena": a real folder, one level under the Inbox, invisible before."""
    client = FakeClient()
    resolution = client.resolve_folder("Quarantena")
    check("resolved", resolution.folder_id, child_id("inbox", "Quarantena"))
    check("matched_by", resolution.matched_by, "inbox_child")
    # It must have looked under msgfolderroot first (and missed) before the Inbox.
    check(
        "searched the Inbox after msgfolderroot",
        [c[1] for c in client.calls if c[2] == 0],
        ["msgfolderroot", "inbox"],
    )


# ------------------------------------------------------------------
# Paging
# ------------------------------------------------------------------


def test_name_found_on_second_page() -> None:
    """A top-level folder past the first page must still resolve.

    "Prj-Alpha" is the last of ~250 top-level folders, so the single 200-entry
    request this replaced could not see it however correct the name was.
    """
    client = FakeClient(page_size=200)
    resolution = client.resolve_folder("Prj-Alpha")
    check("resolved", resolution.folder_id, child_id("msgfolderroot", "Prj-Alpha"))
    check("matched_by", resolution.matched_by, "name")
    offsets = [c[2] for c in client.calls if c[1] == "msgfolderroot"]
    check("paged past the first page", offsets, [0, 200])


def test_short_pages_do_not_end_the_walk() -> None:
    """Only an *empty* page means end-of-list: this backend under-fills pages.

    With 20 folders per response and ~252 top-level folders, stopping at the
    first short page would strand almost everything.
    """
    client = FakeClient(page_size=20)
    resolution = client.resolve_folder("Prj-Alpha")
    check("resolved despite short pages", resolution.folder_id,
          child_id("msgfolderroot", "Prj-Alpha"))
    check(
        "advanced by what arrived, not by the requested page size",
        [c[2] for c in client.calls if c[1] == "msgfolderroot"][:4],
        [0, 20, 40, 60],
    )


def test_offset_ignoring_backend_terminates() -> None:
    """A backend that ignores Offset must yield a truncated answer, not a hang.

    Page 1 arriving forever is indistinguishable from progress unless you look
    for *new* folder ids, so that's the stop condition.
    """
    client = FakeClient(page_size=20, ignore_offset=True)
    resolution = client.resolve_folder("Definitely Not A Folder")
    check("not found", resolution.folder_id, None)
    check("error_code", resolution.error_code, "folder_not_found")
    # Two requests per walk (page 1, then the repeat that adds nothing), over
    # the three walks: msgfolderroot, inbox, msgfolderroot/Deep.
    check("bounded request count", len(client.calls), 6)


# ------------------------------------------------------------------
# Deep fallback and ambiguity
# ------------------------------------------------------------------


def test_deep_unique_name() -> None:
    """A uniquely-named folder anywhere in the mailbox resolves."""
    client = FakeClient()
    resolution = client.resolve_folder("Nested Unique")
    check("resolved", resolution.folder_id, child_id("Deep Parent", "Nested Unique"))
    check("matched_by", resolution.matched_by, "deep_name")


def test_ambiguous_deep_name() -> None:
    """Several matches is refused, with the ids needed to retry."""
    client = FakeClient()
    resolution = client.resolve_folder("Prj-Beta")
    check("no folder chosen", resolution.folder_id, None)
    check("error_code", resolution.error_code, "folder_name_ambiguous")
    check("candidate count", len(resolution.candidates), 2)
    check(
        "candidates carry ids to retry with",
        sorted(c["id"] for c in resolution.candidates),
        sorted([child_id("Deep Parent", "Prj-Beta"), child_id("Other Parent", "Prj-Beta")]),
    )
    check("candidates carry the name", {c["name"] for c in resolution.candidates}, {"Prj-Beta"})


def test_top_level_wins_over_a_deeper_duplicate() -> None:
    """Precedence is the contract: "Prj-Alpha" exists both top-level and nested.

    Resolving to the top-level one is what makes the outcome predictable; the
    deep search is a fallback for names that aren't found higher up, never a
    competitor to them.
    """
    client = FakeClient()
    check("top-level match wins", client.resolve_folder("Prj-Alpha").matched_by, "name")


def test_missing_folder_reports_not_found() -> None:
    client = FakeClient()
    resolution = client.resolve_folder("No Such Folder")
    check("folder_id", resolution.folder_id, None)
    check("error_code", resolution.error_code, "folder_not_found")
    check("no candidates", resolution.candidates, ())


def test_empty_spec() -> None:
    client = FakeClient()
    for spec in ("", "   ", None):
        resolution = client.resolve_folder(spec)
        check(f"{spec!r} -> not found", resolution.error_code, "folder_not_found")
    check("no requests wasted on an empty spec", client.calls, [])


# ------------------------------------------------------------------
# Forms that already worked, pinned so the reordering can't break them
# ------------------------------------------------------------------


def test_distinguished_name_short_circuits_on_canary() -> None:
    """No round-trip on the classic backend: DistinguishedFolderId is already usable."""
    client = FakeClient(auth_mode="canary")
    for name, expected in (("Inbox", "inbox"), ("sent", "sentitems"),
                           ("deleted", "deleteditems"), ("junk", "junkemail")):
        resolution = client.resolve_folder(name)
        check(f"{name!r} -> {expected!r}", resolution.folder_id, expected)
        check(f"{name!r} matched_by", resolution.matched_by, "distinguished")
    check("no requests issued", client.calls, [])


def test_distinguished_name_resolves_on_bearer() -> None:
    """Modern Outlook: FindConversation there needs a real opaque id."""
    client = FakeClient(auth_mode="bearer")
    resolution = client.resolve_folder("Inbox")
    check("resolved via GetFolder", resolution.folder_id, fake_id("inbox"))
    check("matched_by", resolution.matched_by, "distinguished")
    check("one GetFolder", [c[0] for c in client.calls], ["GetFolder"])


def test_path_forms() -> None:
    client = FakeClient()
    check(
        "Inbox/Quarantena",
        client.resolve_folder("Inbox/Quarantena"),
        (child_id("inbox", "Quarantena"), "path", (), None),
    )
    check(
        "Deep Parent/Nested Unique",
        client.resolve_folder("Deep Parent/Nested Unique").folder_id,
        child_id("Deep Parent", "Nested Unique"),
    )
    # A path disambiguates a shared name, which is the documented escape hatch
    # from folder_name_ambiguous alongside passing an id.
    check(
        "Other Parent/Prj-Beta resolves where the bare name is ambiguous",
        client.resolve_folder("Other Parent/Prj-Beta").matched_by,
        "path",
    )


def test_missing_path_segment() -> None:
    client = FakeClient()
    resolution = client.resolve_folder("Deep Parent/No Such Child")
    check("folder_id", resolution.folder_id, None)
    check("matched_by", resolution.matched_by, "path")
    check("error_code", resolution.error_code, "folder_not_found")


def test_bare_distinguished_id_is_accepted_last() -> None:
    """"msgfolderroot"/"deleteditems" are ids, not names, and are only tried
    once every name lookup has missed — so a real folder with that name can
    never be shadowed by one. Same ordering as tasks.py's _resolve_task_folder."""
    client = FakeClient()
    check("msgfolderroot", client.resolve_folder("msgfolderroot").folder_id, "msgfolderroot")
    check("deleteditems", client.resolve_folder("deleteditems").folder_id, "deleteditems")


def test_get_folder_id_wrapper() -> None:
    """The legacy signature every other tool still calls."""
    client = FakeClient()
    check("hit", client.get_folder_id("Quarantena"), child_id("inbox", "Quarantena"))
    check("miss", client.get_folder_id("No Such Folder"), None)
    check("ambiguous is a miss for this signature", client.get_folder_id("Prj-Beta"), None)


def main() -> bool:
    for test in (
        test_looks_like_folder_id,
        test_opaque_id_passes_through_untouched,
        test_opaque_id_containing_slash_is_not_read_as_a_path,
        test_inbox_child_by_name,
        test_name_found_on_second_page,
        test_short_pages_do_not_end_the_walk,
        test_offset_ignoring_backend_terminates,
        test_deep_unique_name,
        test_ambiguous_deep_name,
        test_top_level_wins_over_a_deeper_duplicate,
        test_missing_folder_reports_not_found,
        test_empty_spec,
        test_distinguished_name_short_circuits_on_canary,
        test_distinguished_name_resolves_on_bearer,
        test_path_forms,
        test_missing_path_segment,
        test_bare_distinguished_id_is_accepted_last,
        test_get_folder_id_wrapper,
    ):
        test()

    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return False
    print("test_folder_resolution: all checks passed")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
