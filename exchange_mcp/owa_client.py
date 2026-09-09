"""Centralized OWA client for Exchange API.

Delegates all transport to a BrowserSession (real Chromium tabs) instead
of a raw HTTP client, because OWA now requires signals (fresh per-page
canary, Sec-Fetch headers, real TLS/JS fingerprint) that a hand-rolled
requests.Session replaying exported cookies can't replicate.
"""

from exchange_mcp.browser_session import BrowserSession, SessionExpiredError  # noqa: F401

# Map common folder names (English + Russian) to OWA distinguished folder IDs
DISTINGUISHED_FOLDERS = {
    "inbox": "inbox",
    "входящие": "inbox",
    "sent": "sentitems",
    "отправленные": "sentitems",
    "drafts": "drafts",
    "черновики": "drafts",
    "deleted": "deleteditems",
    "удаленные": "deleteditems",
    "junk": "junkemail",
    "нежелательная почта": "junkemail",
    "outbox": "outbox",
    "исходящие": "outbox",
    "calendar": "calendar",
    "календарь": "calendar",
}

# Full set of Exchange distinguished folder IDs, used to tell apart a
# distinguished ID (own resolved value, or one passed straight through
# by a caller e.g. "msgfolderroot") from a real opaque FolderId when
# building request payloads. Not all of these appear in
# DISTINGUISHED_FOLDERS above - that dict only maps user-facing names
# (incl. Russian aliases) to the ones get_folder_id() commonly resolves.
_DISTINGUISHED_IDS = set(DISTINGUISHED_FOLDERS.values()) | {
    "msgfolderroot", "root", "contacts", "tasks", "notes",
    "journal", "searchfolders", "publicfoldersroot", "favorites",
}


class OWAClient:
    """Public API for OWA JSON calls, folder/name resolution, and attachment downloads.

    All requests go through a shared BrowserSession: each call opens its own
    tab, performs the fetch, and closes it. Session expiry triggers one
    automatic re-login attempt (silent if the persistent profile is still
    signed in, or using cached credentials) before retrying the call once.
    """

    def __init__(self, browser_session: BrowserSession):
        self.browser = browser_session
        self.owa_url = browser_session.owa_url
        self.user_email: str = ""

    @property
    def cookie_file(self):
        """Backward-compat alias: session state now lives in the browser profile dir, not a cookie file."""
        return self.browser.profile_dir

    # ------------------------------------------------------------------
    # Core request methods
    # ------------------------------------------------------------------

    def request(self, action: str, payload: dict, *, timeout: int = 30) -> dict:
        """POST to /owa/service.svc?action={action}&EP=1&ID=-1&AC=1 via a browser tab.

        On session expiry (401, 440, or text/html response), attempts one
        re-login and retries. If that also fails, raises SessionExpiredError.
        """
        for attempt in range(2):
            try:
                return self._to_json(self.browser.post_json(action, payload, timeout=timeout))
            except SessionExpiredError:
                if attempt == 0:
                    self.browser.ensure_logged_in()
                else:
                    raise

        raise SessionExpiredError("Session expired. Call the login tool to log in again.")

    def request_header_payload(self, action: str, payload: dict, *, timeout: int = 30) -> dict:
        """POST with payload in the X-OWA-UrlPostData header (empty body).

        Some OWA actions (CreateFolder, DeleteFolder, RenameFolder, etc.)
        require the JSON payload to be sent as a URL-encoded string in that
        header instead of the POST body. Same retry logic as ``request()``.
        """
        for attempt in range(2):
            try:
                return self._to_json(self.browser.post_header_payload(action, payload, timeout=timeout))
            except SessionExpiredError:
                if attempt == 0:
                    self.browser.ensure_logged_in()
                else:
                    raise

        raise SessionExpiredError("Session expired. Call the login tool to log in again.")

    @staticmethod
    def _to_json(resp) -> dict:
        if resp.status_code in (401, 440):
            raise SessionExpiredError(f"Session expired (HTTP {resp.status_code}).")

        if "text/html" in resp.headers.get("content-type", ""):
            body_snippet = resp.text[:300] if resp.text else ""
            raise SessionExpiredError(
                f"Session expired or invalid action (HTML response, HTTP {resp.status_code}). "
                f"Snippet: {body_snippet}"
            )

        try:
            data = resp.json()
        except (ValueError, TypeError) as exc:
            # A non-JSON, non-HTML body on a non-401/440 status is a server-side
            # fault (e.g. OWA's x-owa-error header carrying a .NET exception name),
            # not a session issue -- don't misreport it as one, since request()'s
            # retry-after-relogin path only helps genuine session expiry and would
            # otherwise just force a pointless extra login check before re-raising.
            owa_error = resp.headers.get("x-owa-error", "")
            detail = f" ({owa_error})" if owa_error else ""
            body_snippet = resp.text[:300] if resp.text else ""
            raise RuntimeError(
                f"OWA request failed (HTTP {resp.status_code}){detail}. Snippet: {body_snippet}"
            ) from exc

        # Some malformed requests fault at the OWA method-dispatch layer
        # instead of the usual per-item ResponseMessages/ResponseClass shape
        # -- e.g. {"Body": {"ErrorCode": 400, "FaultMessage": "..."}} with no
        # "ResponseMessages" key at all. extract_items() finds nothing to
        # iterate in that shape, so callers checking only ResponseClass=="Error"
        # would otherwise treat this as a silent success.
        body = data.get("Body") if isinstance(data, dict) else None
        if isinstance(body, dict) and "ErrorCode" in body and "ResponseMessages" not in body:
            raise RuntimeError(body.get("FaultMessage") or f"OWA request failed (ErrorCode {body['ErrorCode']}).")

        return data

    # ------------------------------------------------------------------
    # File download (attachments)
    # ------------------------------------------------------------------

    def download_file(self, attachment_id: str, *, timeout: int = 60) -> tuple[bytes, str, str]:
        """Download a file attachment by its AttachmentId.

        Returns:
            (content_bytes, filename, content_type)
        """
        resp = self.browser.download_attachment(attachment_id, timeout=timeout)

        if resp.status_code in (401, 440):
            raise SessionExpiredError(f"Session expired (HTTP {resp.status_code}).")

        if "text/html" in resp.headers.get("content-type", ""):
            raise SessionExpiredError("Session expired (HTML response on attachment download).")

        # Parse filename from Content-Disposition header
        filename = "attachment"
        cd = resp.headers.get("content-disposition", "")
        if cd:
            import re as _re
            from urllib.parse import unquote

            # Try filename*= (RFC 5987) first, then filename=
            match = _re.search(r"filename\*=(?:UTF-8''|utf-8'')(.+?)(?:;|$)", cd)
            if match:
                filename = unquote(match.group(1).strip())
            else:
                match = _re.search(r'filename="?([^";]+)"?', cd)
                if match:
                    filename = unquote(match.group(1).strip())

        content_type = resp.headers.get("content-type", "application/octet-stream")

        return resp.content, filename, content_type

    # ------------------------------------------------------------------
    # Convenience: extract response items
    # ------------------------------------------------------------------

    @staticmethod
    def extract_items(data: dict) -> list[dict]:
        """Extract Items from standard OWA response envelope.

        Response shape: data["Body"]["ResponseMessages"]["Items"]
        """
        try:
            return data["Body"]["ResponseMessages"]["Items"]
        except (KeyError, TypeError):
            return []

    # ------------------------------------------------------------------
    # Folder helpers
    # ------------------------------------------------------------------

    @staticmethod
    def folder_id_dict(folder_id: str) -> dict:
        """Build the typed folder-reference dict for a folder_id.

        get_folder_id() returns the bare distinguished ID (e.g. "inbox")
        for distinguished folders instead of a resolved opaque ID, so
        callers must use this instead of hardcoding FolderId - a
        distinguished name needs the DistinguishedFolderId wrapper.
        """
        if folder_id.lower() in _DISTINGUISHED_IDS:
            return {"__type": "DistinguishedFolderId:#Exchange", "Id": folder_id}
        return {"__type": "FolderId:#Exchange", "Id": folder_id}

    def get_folder_id(self, folder_name: str) -> str | None:
        """Resolve a folder name to its Exchange folder ID.

        Supports distinguished folder names (inbox, sentitems, drafts, etc.)
        in both English and Russian, plus custom folder names looked up
        via FindFolder on msgfolderroot, plus "/"-delimited paths
        (e.g. "Progetti/ACE-NewGeco" or "Inbox/Quarantena") for folders
        nested more than one level deep - see _resolve_folder_path().

        Distinguished folders are normally returned as-is (e.g. "inbox")
        without a GetFolder round-trip: on the classic canary-cookie OWA
        backend, GetFolder returns a flattened {"Folders": [...]} shape with
        no FolderId at all, but DistinguishedFolderId is already a valid
        identifier everywhere a resolved FolderId would be used - see
        folder_id_dict(). On the modern OAuth/Bearer backend ("new Outlook"),
        GetFolder works correctly and FindConversation there requires a real
        opaque FolderId rather than the bare distinguished name, so we
        resolve it properly in that mode instead of short-circuiting.
        """
        if "/" in folder_name:
            return self._resolve_folder_path(folder_name)

        folder_lower = folder_name.lower()

        distinguished_id = DISTINGUISHED_FOLDERS.get(folder_lower)
        if distinguished_id:
            if self.browser.auth_mode != "bearer":
                return distinguished_id
            return self._resolve_distinguished_folder_id(distinguished_id) or distinguished_id

        root_ref = {"__type": "DistinguishedFolderId:#Exchange", "Id": "msgfolderroot"}
        return self._find_child_folder_id(root_ref, folder_name)

    def _resolve_folder_path(self, folder_path: str) -> str | None:
        """Resolve a "/"-delimited folder path by walking one Shallow
        FindFolder per segment.

        get_folder_id()'s plain-name lookup only searches direct children
        of msgfolderroot (Shallow traversal), so a folder nested under
        another custom folder (e.g. "ACE-NewGeco" under "Progetti") or
        under a distinguished folder (e.g. "Quarantena" under "Inbox") is
        invisible to it - confirmed live: neither the bare name nor a
        literal "Progetti/ACE-NewGeco" string (which can never equal a
        single-segment DisplayName) matched. Walking the path segment by
        segment, resolving each as a child of the previous, handles any
        depth and disambiguates same-named folders living at different
        levels (this mailbox has both a top-level "ACE - NewGeco" and a
        nested "ACE-NewGeco" under "Progetti").
        """
        segments = [s for s in folder_path.split("/") if s]
        if not segments:
            return None

        first = segments[0].lower()
        distinguished_id = DISTINGUISHED_FOLDERS.get(first)
        if distinguished_id:
            parent_ref = {"__type": "DistinguishedFolderId:#Exchange", "Id": distinguished_id}
            segments = segments[1:]
        else:
            parent_ref = {"__type": "DistinguishedFolderId:#Exchange", "Id": "msgfolderroot"}

        folder_id = None
        for segment in segments:
            folder_id = self._find_child_folder_id(parent_ref, segment)
            if folder_id is None:
                return None
            parent_ref = {"__type": "FolderId:#Exchange", "Id": folder_id}

        return folder_id if segments else parent_ref.get("Id")

    def _find_child_folder_id(self, parent_ref: dict, child_name: str) -> str | None:
        """Shallow FindFolder for a single child folder by DisplayName under parent_ref."""
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
                    "BaseShape": "Default",
                },
                "ParentFolderIds": [parent_ref],
                "Traversal": "Shallow",
                "Paging": {
                    "__type": "IndexedPageView:#Exchange",
                    "BasePoint": "Beginning",
                    "Offset": 0,
                    "MaxEntriesReturned": 200,
                },
            },
        }

        data = self.request("FindFolder", payload)
        child_lower = child_name.lower()
        for msg in self.extract_items(data):
            if "RootFolder" in msg and "Folders" in msg["RootFolder"]:
                for f in msg["RootFolder"]["Folders"]:
                    if f.get("DisplayName", "").lower() == child_lower:
                        return f.get("FolderId", {}).get("Id")

        return None

    def _resolve_distinguished_folder_id(self, distinguished_id: str) -> str | None:
        """Resolve a distinguished folder name to its real opaque FolderId via GetFolder.

        Only meaningful on backends where GetFolder returns the classic
        Items/FolderId envelope (the modern OAuth/Bearer backend) - see
        get_folder_id().
        """
        payload = {
            "__type": "GetFolderJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "GetFolderRequest:#Exchange",
                "FolderShape": {
                    "__type": "FolderResponseShape:#Exchange",
                    "BaseShape": "IdOnly",
                },
                "FolderIds": [
                    {"__type": "DistinguishedFolderId:#Exchange", "Id": distinguished_id}
                ],
            },
        }
        data = self.request("GetFolder", payload)
        for msg in self.extract_items(data):
            folders = msg.get("Folders", [])
            if folders:
                return folders[0].get("FolderId", {}).get("Id")
        return None

    # ------------------------------------------------------------------
    # ResolveNames (directory search / attendee resolution)
    # ------------------------------------------------------------------

    def resolve_names(
        self, query: str, *, full_contact: bool = True
    ) -> list[dict]:
        """Call ResolveNames to search the directory.

        Returns the list of Resolution dicts from the API, each containing
        Mailbox and optionally Contact data.
        """
        payload = {
            "__type": "ResolveNamesJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "ResolveNamesRequest:#Exchange",
                "UnresolvedEntry": query,
                "ReturnFullContactData": full_contact,
                "SearchScope": "ActiveDirectoryContacts",
                "ContactDataShape": "AllProperties" if full_contact else "Default",
            },
        }

        data = self.request("ResolveNames", payload)
        for msg in self.extract_items(data):
            if "ResolutionSet" in msg and "Resolutions" in msg["ResolutionSet"]:
                return msg["ResolutionSet"]["Resolutions"]

        return []
