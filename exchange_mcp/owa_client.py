"""Centralized OWA client for Exchange API.

Delegates all transport to a BrowserSession (real Chromium tabs) instead
of a raw HTTP client, because OWA now requires signals (fresh per-page
canary, Sec-Fetch headers, real TLS/JS fingerprint) that a hand-rolled
requests.Session replaying exported cookies can't replicate.
"""

import re
from datetime import datetime
from typing import Iterator, NamedTuple
from urllib.parse import quote

from exchange_mcp import mailbox_identity
from exchange_mcp.auth_errors import (  # noqa: F401
    INTERACTIVE_LOGIN_REQUIRED,
    AuthenticationRequiredError,
)
from exchange_mcp.browser_session import (  # noqa: F401
    BearerModeRequiredError,
    BrowserSession,
    CopilotUnavailableError,
    SessionExpiredError,
)
from exchange_mcp.mailbox_timezone import (
    MailboxTimezone,
    parse_mailbox_timezone_id,
    request_header as _build_request_header,
    resolve_mailbox_timezone,
)

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
    # The Tasks folder is what the modern web UI shows as Microsoft To Do, and
    # each To Do list is a child folder of it — so this entry is what lets
    # _resolve_folder_path() walk "tasks/<list name>" (see tools/tasks.py).
    "tasks": "tasks",
    "задачи": "tasks",
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

# How many folders one FindFolder page asks for. This backend does not
# reliably honour MaxEntriesReturned, so it's a hint rather than a contract --
# which is exactly why _iter_child_folders() pages instead of trusting one
# response to be complete.
_FOLDER_PAGE_SIZE = 200

# Hard stop on the paging loop. A mailbox with more folders than this
# (50 * 200 = 10 000) is beyond what folder-name resolution can usefully
# serve anyway, and the cap means a backend that mishandles Offset in some
# new way degrades into a truncated answer instead of an endless loop.
_FOLDER_PAGE_LIMIT = 50

# A base64-ish blob this long is an EWS folder ID, not a DisplayName. The
# charset check is what makes it safe: a display name that long would
# essentially always contain a space or punctuation outside this set.
_FOLDER_ID_SHAPE = re.compile(r"^[A-Za-z0-9+/=_-]+$")


def looks_like_folder_id(value: str) -> bool:
    """True if `value` is an opaque EWS folder ID rather than a folder name.

    Every folder resolver has to ask this *first*, before any name or path
    handling: EWS folder IDs are long base64 blobs that routinely contain
    "/" (verified live on this tenant), so a resolver that splits on "/" to
    walk a path would otherwise chop a perfectly good ID into nonsense
    segments and report the folder as missing. That's precisely the bug
    behind "passing the exact ID from get_folders also fails" -- see
    OWAClient.resolve_folder().
    """
    value = (value or "").strip()
    return len(value) > 80 and bool(_FOLDER_ID_SHAPE.match(value))


class FolderResolution(NamedTuple):
    """Outcome of resolving a caller-supplied folder name / path / ID.

    `folder_id` is None exactly when resolution failed, and then
    `error_code` says which of the two failure modes it was, because they
    need different fixes from the caller:

    - "folder_not_found"      -- nothing anywhere in the mailbox matched.
    - "folder_name_ambiguous" -- several folders share that display name, so
      picking one would be a coin flip. `candidates` then carries every
      match (name + id + item count) so the caller can re-issue the call
      with an id, which always resolves unambiguously.

    `matched_by` records *how* it resolved ("folder_id", "path",
    "distinguished", "name", "inbox_child", "deep_name"). Tools surface it
    on success so an unattended run's logs show which folder was actually
    hit rather than just that something was.
    """

    folder_id: str | None
    matched_by: str
    candidates: tuple[dict, ...] = ()
    error_code: str | None = None


class OWAClient:
    """Public API for OWA JSON calls, folder/name resolution, and attachment downloads.

    All requests go through a shared BrowserSession: each call opens its own
    tab, performs the fetch, and closes it. Session expiry triggers one silent
    re-auth attempt against the persistent profile before retrying the call
    once; if the profile can't carry us either, it raises
    AuthenticationRequiredError telling the caller to use the `login` tool --
    there are no stored credentials to log in with. See _relogin_or_raise().
    """

    def __init__(self, browser_session: BrowserSession):
        self.browser = browser_session
        self.owa_url = browser_session.owa_url
        # Resolved lazily by mailbox_address(), then cached for the process --
        # never assigned here. The predecessor of that method was a plain
        # `self.user_email = ""` attribute whose only writers lived in the
        # credential store; see mailbox_identity's module docstring for what
        # removing them broke and for how long.
        self._mailbox_address: mailbox_identity.MailboxAddress | None = None
        # Probed once, lazily, on first use -- see mailbox_timezone().
        self._mailbox_timezone: MailboxTimezone | None = None
        # The GetOwaUserConfiguration response, shared by the two things that
        # read it (our own address and our own timezone) so the process makes
        # that request at most once. `_probed` is separate from the value
        # because None is a real answer -- "this backend won't serve it" -- and
        # caching it is the point: the realistic failure is a backend with no
        # such surface, which will not grow one mid-process.
        self._owa_user_config: dict | None = None
        self._owa_user_config_probed: bool = False

    @property
    def cookie_file(self):
        """Backward-compat alias: session state now lives in the browser profile dir, not a cookie file."""
        return self.browser.profile_dir

    # ------------------------------------------------------------------
    # Re-login on session expiry
    # ------------------------------------------------------------------

    def _relogin_or_raise(self) -> None:
        """Silently re-acquire the session on a 401/440, or raise for a human.

        The silent path is all this server has: it re-checks the persistent
        profile, which can still carry us through on live OWA cookies, the "stay
        signed in" cookie, or an SSO session the SPA can mint a fresh Bearer
        token from. There is no stored password to replay.

        When that fails, retrying is pointless, so this raises
        AuthenticationRequiredError rather than letting the caller loop. It
        deliberately does *not* pop up a sign-in window here: that would mean a
        Chromium window appearing in the middle of some unrelated tool call.
        Opening the window is the `login` tool's job, which the error text points
        the caller at.
        """
        result = self.browser.ensure_logged_in()
        if result.get("success"):
            return
        raise AuthenticationRequiredError(
            result.get("error") or "The browser profile has no usable OWA session.",
            result.get("reason") or INTERACTIVE_LOGIN_REQUIRED,
        )

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
                    self._relogin_or_raise()
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
                    self._relogin_or_raise()
                else:
                    raise

        raise SessionExpiredError("Session expired. Call the login tool to log in again.")

    def request_substrate(
        self, path_and_query: str, extra_headers: dict, payload: dict, *, timeout: int = 30
    ) -> dict:
        """POST to a modern-Outlook REST surface (search/PeopleGraphVx, etc.)
        instead of an EWS action on /owa/service.svc - see
        BrowserSession._async_post_substrate. Same retry-once-on-expiry
        behavior as request(). Raises RuntimeError if the browser session
        isn't in bearer auth mode (this surface doesn't exist on classic OWA).
        """
        for attempt in range(2):
            try:
                return self._to_json(
                    self.browser.post_substrate(path_and_query, extra_headers, payload, timeout=timeout)
                )
            except SessionExpiredError:
                if attempt == 0:
                    self._relogin_or_raise()
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
    # Own mailbox identity
    # ------------------------------------------------------------------

    def resolve_own_mailbox(self, *, refresh: bool = False) -> mailbox_identity.MailboxAddress:
        """Our own mailbox's SMTP address, with the signal it came from.

        Resolved on first use and cached for the process. Costs at most one
        request (`GetOwaUserConfiguration`) ever, and often none at all: in
        bearer mode the `x-anchormailbox` header captured with the session's
        token usually answers it outright.

        **Degrades, never raises.** Every caller here is a tool that has
        something useful to do without the address (`find_free_time` reads the
        calendar folder directly; `get_schedule` can send an attendee's id as
        the requesting user), so a mailbox whose backend won't tell us who we
        are must not take those tools offline. `MailboxAddress.reason` says why
        it's empty, for tools that report it. The one thing this must never do
        is *guess*: `get_meeting_contacts` excludes "self" by comparing against
        this value, so a wrong address silently keeps the user in their own
        contact ranking, while an absent one is visible.

        Failures are cached too, deliberately. The realistic failure is a
        backend with no such surface, which will not start having one mid-
        process, and re-probing per call would add a request to every
        availability call for the life of the server. `refresh=True` is for the
        one case where the answer can genuinely change: an interactive
        `login(force=True)` that switched accounts.
        """
        if self._mailbox_address is not None and not refresh:
            return self._mailbox_address

        hints = self.browser.identity_hints()
        resolved = mailbox_identity.resolve_mailbox_address(
            anchor_mailbox=hints.get("anchor_mailbox", ""),
            bearer_token=hints.get("bearer_token", ""),
        )

        if not resolved.address:
            config = self._get_owa_user_configuration()
            # Re-read the hints afterwards even when the config call failed:
            # that call is what establishes auth on a session whose first tool
            # call this is, so a bearer capture (and with it the anchormailbox
            # header) may only exist now. Free, since both live in memory.
            hints = self.browser.identity_hints()
            resolved = mailbox_identity.resolve_mailbox_address(
                anchor_mailbox=hints.get("anchor_mailbox", ""),
                user_configuration=config,
                bearer_token=hints.get("bearer_token", ""),
            )

        self._mailbox_address = resolved
        return resolved

    def mailbox_address(self, *, refresh: bool = False) -> str:
        """Our own mailbox's SMTP address, or "" if this session can't say.

        Thin wrapper over resolve_own_mailbox() for the callers that only need
        the address itself. Same caching and same never-raises contract.
        """
        return self.resolve_own_mailbox(refresh=refresh).address

    def forget_mailbox_address(self) -> None:
        """Drop everything cached about *which* mailbox this is, so the next
        read re-resolves it.

        Called after an interactive sign-in: `login(force=True)` exists to
        switch accounts, and a cached address from the *previous* account is
        the one failure mode worse than having none.

        That reasoning covers the timezone and the shared config response as
        well, so both are dropped here despite the name: the new account can be
        in a different zone, and a stale one would silently shift every
        free/busy answer by the difference between the two -- the #601 bug
        again, arrived at from the other direction. Kept as one method rather
        than three because there is exactly one event that invalidates them
        (the account changed) and a caller that forgot one of three calls would
        reintroduce precisely this.
        """
        self._mailbox_address = None
        self._mailbox_timezone = None
        self._owa_user_config = None
        self._owa_user_config_probed = False

    def _get_owa_user_configuration(self) -> dict | None:
        """Fetch GetOwaUserConfiguration, or None if this backend won't serve it.

        This is OWA's own bootstrap call for the signed-in user's settings, so
        it exists wherever a mailbox does, but its response shape isn't pinned
        down across the classic and modern backends (which is why the parsing
        in mailbox_identity searches by key name rather than by path). Errors
        are swallowed on purpose, including AuthenticationRequiredError: if the
        session really is dead, the caller's own next request raises it with
        the remediation text attached, and this identity probe is not the place
        to surface that.

        **Memoised, because it now has two consumers.** This response carries
        both our own address (`resolve_own_mailbox`) and our own timezone
        (`mailbox_timezone`), and each was added by a separate change that
        introduced its own probe of the same action -- so without this the
        second one silently doubled the request. Sharing it here rather than
        having one caller pass the dict to the other keeps both entry points
        independently callable and order-independent, which matters because
        either can be the first tool call of a session.

        A None result is cached too, for the reason `resolve_own_mailbox`
        states about its own failures: the realistic failure is a backend with
        no such surface, and re-probing per call would add a request to every
        availability call for the life of the server.
        """
        if self._owa_user_config_probed:
            return self._owa_user_config

        payload = {
            "__type": "GetOwaUserConfigurationJsonRequest:#Exchange",
            "Header": {
                "__type": "JsonRequestHeaders:#Exchange",
                "RequestServerVersion": "Exchange2013",
            },
            "Body": {
                "__type": "GetOwaUserConfigurationRequest:#Exchange",
            },
        }
        try:
            data = self.request("GetOwaUserConfiguration", payload)
        except Exception:
            data = None

        self._owa_user_config = data if isinstance(data, dict) else None
        self._owa_user_config_probed = True
        return self._owa_user_config

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
        """Resolve a folder name / path / ID to an Exchange folder ID, or None.

        Thin wrapper over resolve_folder() for the many call sites that only
        need "did it resolve?". Anything reporting an error to a user should
        call resolve_folder() directly instead and surface its `error_code` /
        `candidates`: "not found" and "that name means three different
        folders" are not the same problem, and this signature can't tell
        them apart.
        """
        return self.resolve_folder(folder_name).folder_id

    def resolve_folder(self, folder_spec: str) -> FolderResolution:
        """Resolve a folder spec - an ID, a "/"-path, or a display name.

        Accepted forms, tried in this order (the order is the contract; each
        tier exists because the one above it provably isn't enough):

        1. **An opaque folder ID** as returned by `get_folders`, passed
           through untouched. This is the authoritative form and is
           guaranteed to work: it needs no lookup, so it can't be defeated
           by duplicate names, nesting, or paging. It has to be checked
           first because those IDs contain "/" and would otherwise be
           mistaken for a path (see looks_like_folder_id()).
        2. **A "/"-delimited path** ("Progetti/ClientFolder",
           "Inbox/Quarantena"), walked one Shallow FindFolder per segment -
           see _resolve_folder_path(). Use this to disambiguate a name that
           several folders share.
        3. **A distinguished folder name** ("inbox", "sent", "deleted", ...
           English or Russian). Normally returned as-is, without a GetFolder
           round-trip: on the classic canary-cookie backend GetFolder
           returns a flattened {"Folders": [...]} shape with no FolderId at
           all, while DistinguishedFolderId is already valid everywhere a
           resolved FolderId would be used (see folder_id_dict()). On the
           modern OAuth/Bearer backend GetFolder does work, and
           FindConversation there insists on a real opaque FolderId, so it
           is resolved properly in that mode.
        4. **A display name of a direct child of msgfolderroot** (a
           top-level folder).
        5. **A display name of a direct child of the Inbox.** Cheap, and it
           covers the single most common place a user puts folders -- the
           reported failure ("Quarantena", live-confirmed to be an Inbox
           child, not the top-level folder it appeared to be in a recursive
           listing) landed exactly here.
        6. **A display name found anywhere in the mailbox** (one Deep
           FindFolder), accepted only when *exactly one* folder matches.
           Several matches is reported as "folder_name_ambiguous" with every
           candidate rather than silently resolved to whichever the server
           listed first - this mailbox has many similar short names
           ("Prj-*"), and quietly filing mail into the wrong one of them is
           worse than failing.
        7. **A bare distinguished ID** ("msgfolderroot", "deleteditems", ...)
           - last, so that a technical ID can never shadow a real folder
           that happens to share the name. Same reasoning as
           tools/tasks.py's _resolve_task_folder().
        """
        spec = (folder_spec or "").strip()
        if not spec:
            return FolderResolution(None, "none", (), "folder_not_found")

        # 1. Opaque folder ID -- before the "/" split, or IDs containing "/"
        #    get chopped into path segments and never match anything.
        if looks_like_folder_id(spec):
            return FolderResolution(spec, "folder_id")

        # 2. "/"-delimited path
        if "/" in spec:
            folder_id = self._resolve_folder_path(spec)
            if folder_id:
                return FolderResolution(folder_id, "path")
            return FolderResolution(None, "path", (), "folder_not_found")

        # 3. Distinguished folder name
        distinguished_id = DISTINGUISHED_FOLDERS.get(spec.lower())
        if distinguished_id:
            return FolderResolution(
                self._distinguished_folder_ref_id(distinguished_id), "distinguished"
            )

        root_ref = {"__type": "DistinguishedFolderId:#Exchange", "Id": "msgfolderroot"}

        # 4. Top-level folder by display name
        folder_id = self._find_child_folder_id(root_ref, spec)
        if folder_id:
            return FolderResolution(folder_id, "name")

        # 5. Inbox child by display name
        inbox_ref = {"__type": "DistinguishedFolderId:#Exchange", "Id": "inbox"}
        folder_id = self._find_child_folder_id(inbox_ref, spec)
        if folder_id:
            return FolderResolution(folder_id, "inbox_child")

        # 6. Anywhere in the mailbox, unique match only
        matches = self._find_child_folders_by_name(root_ref, spec, traversal="Deep")
        if len(matches) == 1:
            return FolderResolution(matches[0]["id"], "deep_name")
        if len(matches) > 1:
            return FolderResolution(None, "deep_name", tuple(matches), "folder_name_ambiguous")

        # 7. Bare distinguished ID, only once every name lookup has missed.
        if spec.lower() in _DISTINGUISHED_IDS:
            return FolderResolution(
                self._distinguished_folder_ref_id(spec.lower()), "distinguished"
            )

        return FolderResolution(None, "name", (), "folder_not_found")

    def _distinguished_folder_ref_id(self, distinguished_id: str) -> str:
        """Return the identifier to use for a distinguished folder in this auth mode.

        See resolve_folder() step 3 for why the two backends differ.
        """
        if self.browser.auth_mode != "bearer":
            return distinguished_id
        return self._resolve_distinguished_folder_id(distinguished_id) or distinguished_id

    def _resolve_folder_path(self, folder_path: str) -> str | None:
        """Resolve a "/"-delimited folder path by walking one Shallow
        FindFolder per segment.

        Originally added because plain-name lookup searched direct children
        of msgfolderroot only, so a folder nested under another custom
        folder (e.g. "ClientFolder" under "Projects") or under a
        distinguished folder (e.g. "Triage" under "Inbox") was invisible to
        it - confirmed live: neither the bare name nor a literal
        "Projects/ClientFolder" string (which can never equal a
        single-segment DisplayName) matched.

        resolve_folder() now also falls back to an Inbox-child lookup and a
        unique Deep match, so a path is no longer the *only* way to reach a
        nested folder. It stays the way to reach an unambiguous *specific*
        one: walking segment by segment disambiguates same-named folders at
        different levels (this mailbox has both a top-level "Client -
        Folder" and a nested "ClientFolder" under "Projects"), which is
        exactly what a bare name cannot do - and what a
        "folder_name_ambiguous" result asks the caller to supply, alongside
        passing an id.
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

    def find_folder_page(
        self, parent_ref: dict, *, traversal: str = "Shallow", offset: int = 0
    ) -> list[dict]:
        """One page of raw FindFolder results (the API's own Folder dicts).

        Public because the tools that *list* folders need the same paging as
        the ones that resolve a name - see iter_child_folders().
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
                    "BaseShape": "Default",
                },
                "ParentFolderIds": [parent_ref],
                "Traversal": traversal,
                "Paging": {
                    "__type": "IndexedPageView:#Exchange",
                    "BasePoint": "Beginning",
                    "Offset": offset,
                    "MaxEntriesReturned": _FOLDER_PAGE_SIZE,
                },
            },
        }

        data = self.request("FindFolder", payload)
        folders: list[dict] = []
        for msg in self.extract_items(data):
            if "RootFolder" in msg and "Folders" in msg["RootFolder"]:
                folders.extend(msg["RootFolder"]["Folders"])
        return folders

    def iter_child_folders(
        self, parent_ref: dict, *, traversal: str = "Shallow"
    ) -> Iterator[dict]:
        """Yield every child folder under parent_ref, paging with the server's Offset.

        Three rules here, each of them load-bearing on this backend:

        - **Only an empty page ends the walk.** MaxEntriesReturned is not
          honoured reliably, so a short page proves nothing (the same rule
          email.py's conversation paging follows). One wasted request at the
          end beats silently truncating the folder list - which is what the
          single 200-entry request this replaces did: a mailbox with more
          folders than one page simply couldn't resolve the ones past it.
        - **The next Offset advances by what actually arrived**, not by
          _FOLDER_PAGE_SIZE, so a short page doesn't skip folders.
        - **A page that adds no new folder IDs also ends the walk.** If a
          backend ignores Offset it would otherwise hand back page 1 forever;
          stopping on "nothing new" turns that into a truncated answer rather
          than an infinite loop. _FOLDER_PAGE_LIMIT backstops both.
        """
        offset = 0
        seen: set[str] = set()
        for _ in range(_FOLDER_PAGE_LIMIT):
            page = self.find_folder_page(parent_ref, traversal=traversal, offset=offset)
            if not page:
                return
            fresh = 0
            for folder in page:
                key = folder.get("FolderId", {}).get("Id", "")
                if key:
                    if key in seen:
                        continue
                    seen.add(key)
                fresh += 1
                yield folder
            if fresh == 0:
                return
            offset += len(page)

    def _find_child_folder_id(self, parent_ref: dict, child_name: str) -> str | None:
        """First child folder under parent_ref whose DisplayName matches, or None."""
        child_lower = child_name.lower()
        for folder in self.iter_child_folders(parent_ref):
            if folder.get("DisplayName", "").lower() == child_lower:
                return folder.get("FolderId", {}).get("Id")
        return None

    def _find_child_folders_by_name(
        self, parent_ref: dict, name: str, *, traversal: str = "Shallow"
    ) -> list[dict]:
        """Every folder under parent_ref matching `name`, as candidate dicts.

        Returns the same {name, id, total_count} shape tools report back to
        the caller, so an ambiguous match can be handed straight to them as
        a list of ids to choose from.
        """
        name_lower = name.lower()
        matches: list[dict] = []
        for folder in self.iter_child_folders(parent_ref, traversal=traversal):
            if folder.get("DisplayName", "").lower() != name_lower:
                continue
            matches.append({
                "name": folder.get("DisplayName", ""),
                "id": folder.get("FolderId", {}).get("Id", ""),
                "total_count": folder.get("TotalCount", 0),
            })
        return matches

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

    # ------------------------------------------------------------------
    # Substrate people search (modern Outlook backend only)
    # ------------------------------------------------------------------

    def find_people(self, query: str, *, size: int = 25) -> list[dict]:
        """Search the directory via the People app's own search API.

        ResolveNames throws a server-side NullReferenceException on this
        tenant (PROJECT_STATUS.md #401) - confirmed unfixable client-side.
        outlook.cloud.microsoft/people's search box doesn't call
        ResolveNames at all: it hits /search/api/v1/suggestions, a
        Substrate Search endpoint that's a completely separate code path
        from EWS and happens to work. Only usable in bearer auth mode -
        callers should fall back to resolve_names() on classic OWA.

        Returns the raw list of Suggestion dicts (DisplayName,
        EmailAddresses, CompanyName, Department, OfficeLocation, JobTitle,
        Phones, Alias, ADObjectId, etc.) - shallower than a ResolveNames
        Contact (no manager/direct-reports/postal address), but it's real
        data instead of a guaranteed 500.
        """
        payload = {
            "Cvid": "00000000-0000-0000-0000-000000000000",
            "EntityRequests": [{
                "EntityType": "People",
                "Query": {"QueryString": query},
                "Size": size,
                "Provenances": ["Mailbox", "Directory"],
                "Fields": [
                    "Id", "DisplayName", "EmailAddresses", "PeopleType", "PeopleSubtype",
                    "PersonaId", "ADObjectId", "CompanyName", "Department", "OfficeLocation",
                    "JobTitle", "ImAddress", "GivenName", "Surname", "Alias", "Phones",
                    "UserPrincipalName",
                ],
                "Filter": {"And": [
                    {"Or": [{"Term": {"PeopleType": "Person"}}, {"Term": {"PeopleType": "Group"}}]},
                    {"Or": [
                        {"Term": {"PeopleSubtype": "OrganizationUser"}},
                        {"Term": {"PeopleSubtype": "OrganizationContact"}},
                        {"Term": {"PeopleSubtype": "PersonalContact"}},
                        {"Term": {"PeopleSubtype": "PersonalDistributionList"}},
                    ]},
                ]},
            }],
            "Scenario": {"Name": "owa.react.people"},
        }
        headers = {
            "x-ms-appname": "owa-reactpeople",
            "owaappid": "9199bf20-a13f-4107-85dc-02114787ef48",
            "prefer": 'IdType="ImmutableId", exchange.behavior="IncludeThirdPartyOnlineMeetingProviders"',
        }
        data = self.request_substrate("/search/api/v1/suggestions?domain=People", headers, payload)
        suggestions = []
        for group in data.get("Groups", []):
            suggestions.extend(group.get("Suggestions", []))
        return suggestions

    # ------------------------------------------------------------------
    # Mailbox timezone (probed once per process)
    # ------------------------------------------------------------------

    def request_header(self, server_version: str, *, with_timezone: bool = True) -> dict:
        """The `JsonRequestHeaders` block for a request, carrying the mailbox's zone.

        Every request builder in this package should come through here rather
        than writing its own `TimeZoneContext` — that is what let one wrong zone
        live in nine copies, two of them module-level constants no per-call
        value could reach (issue #8).

        `with_timezone=False` omits the context, which the task *reads* require:
        their UTC-midnight dates must not be converted or they come back a day
        off. See `mailbox_timezone.request_header`, which explains that trap in
        full, and note that resolving the zone costs nothing extra — it is the
        cached probe `resolve_own_mailbox` already paid for.
        """
        return _build_request_header(
            server_version, self.mailbox_timezone() if with_timezone else None
        )

    def mailbox_timezone(self, *, refresh: bool = False) -> MailboxTimezone:
        """The mailbox's timezone -- the frame every wall-clock number means.

        Probed once per process and cached, exactly like `resolve_own_mailbox()`
        and off the *same* `GetOwaUserConfiguration` response (see
        `_get_owa_user_configuration`, which memoises it so having two
        consumers costs one request, not two). It is a per-mailbox server fact
        that does not change under us, and the tools that need it
        (`find_free_time` #601, `find_meeting_time` #602) would otherwise pay a
        round-trip per call for an answer that never differs.

        Never raises for a timezone reason and never returns None -- the
        fallback chain in `resolve_mailbox_timezone()` is the contract, and
        `MailboxTimezone.source` / `.warning` say which step it took so the
        tools can report it. See exchange_mcp/mailbox_timezone.py for why a
        shifted-but-working answer beats an error here. Note that a session too
        dead to answer the config call lands in that chain rather than raising:
        the caller's own next request surfaces the auth error with its
        remediation text, which is the same division of labour
        `_get_owa_user_configuration` documents for the address.

        `refresh=True` mirrors `resolve_own_mailbox`: only an interactive
        `login(force=True)` that switched accounts can change the answer.
        """
        if self._mailbox_timezone is None or refresh:
            self._mailbox_timezone = resolve_mailbox_timezone(
                parse_mailbox_timezone_id(self._get_owa_user_configuration())
            )
        return self._mailbox_timezone

    # ------------------------------------------------------------------
    # Substrate GetSchedule (modern Outlook backend only - free/busy)
    # ------------------------------------------------------------------

    _GET_SCHEDULE_QUERY = """query GetSchedule($input: GetScheduleInput) {
  getSchedule(request: $input) {
    schedules {
      availabilityView
      error { message responseCode diagnosticData }
      scheduleId
      scheduleItems {
        location
        status
        subject
        isRecurring
        startTime { dateTime }
        endTime { dateTime }
      }
    }
  }
}"""

    @staticmethod
    def _parse_schedule_dt(t: dict | None):
        """Parse a GetSchedule {"dateTime": ..., "timeZone": {...}} node.

        Unlike availabilityView (wall-clock in the requested tz_id),
        scheduleItems' startTime/endTime always come back UTC-offset
        (confirmed live - "Z"/"+00:00" regardless of the requested
        tz_id). Stripped to naive the same way the rest of this module
        already treats GetUserAvailability's CalendarEventArray timestamps
        (see the pre-existing _get_availability_events in
        tools/availability.py): **the result is a naive UTC instant, not a
        local time**, and a caller that compares it against a wall-clock
        number has to convert it first.

        That last sentence is not a style note. This docstring used to call
        the convention "established (if imprecise)", and the imprecision was
        a real, shipped bug: find_free_time/find_meeting_time subtracted
        these instants from a 9-to-18 *local* working-day window and reported
        every free slot shifted by the mailbox's UTC offset (PROJECT_STATUS.md
        #601/#602, fixed 2026-09-16). The conversion now has exactly one
        home -- exchange_mcp/mailbox_timezone.py, MailboxTimezone.from_utc()
        -- so a new caller has something to reach for instead of a convention
        to re-misread. This function's own contract is unchanged, on purpose:
        every other caller compares these instants only against each other,
        where naive UTC is correct and cheapest.

        Fractional seconds can run to 7 digits (.NET ticks), one more than
        datetime.fromisoformat's 6-digit limit.
        """
        if not t:
            return None
        raw = t.get("dateTime", "")
        if not raw:
            return None
        # Trim fractional digits beyond microsecond precision (datetime.fromisoformat's
        # limit) wherever they fall, before the Z/offset suffix rather than at a fixed
        # string offset - .NET ticks can run to 7 digits.
        raw = re.sub(r"(\.\d{6})\d+", r"\1", raw).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=None)
        except ValueError:
            return None

    def get_schedule(
        self, emails: list[str], start: datetime, end: datetime, *,
        interval_minutes: int = 30, tz_id: str | None = None,
    ) -> list[dict]:
        """Fetch free/busy via the modern Outlook Scheduling Assistant's own
        GetSchedule GraphQL query, instead of the broken EWS
        GetUserAvailability action (PROJECT_STATUS.md #602: it returns a
        server-side NotImplementedException on this tenant - confirmed via
        live capture that the Scheduling Assistant UI itself doesn't call it
        either, it calls this GraphQL operation on the same
        outlookgatewayb2/graphql gateway already used for bearer-mode auth).
        Only usable in bearer auth mode - callers should fall back to the
        legacy GetUserAvailability action on classic OWA.

        Returns one dict per input email, in input order:
        {"email", "availability_view" (same 0/1/2/3/4-per-interval encoding
        as GetUserAvailability's MergedFreeBusy - existing parsers apply
        unchanged), "error" (per-mailbox error dict or None), "events"
        (list of {"start", "end", "subject", "status", "is_recurring"},
        including free-status items - callers filter as they already do
        for CalendarEventArray).

        **The two returned shapes are in different frames**, which is the whole
        reason `tz_id` matters and why it no longer has a literal default:
        `events` timestamps are UTC (see _parse_schedule_dt), while
        `availability_view`'s character positions are wall-clock offsets from
        `start` *as expressed in `tz_id`*. `tz_id=None` means "the mailbox's
        own timezone" (`mailbox_timezone().wire_id`), which is what OWA's own
        client sends and what makes the two consistent for a caller that
        converts `events` out of UTC. It used to default to the literal
        "Russian Standard Time" -- a stray UTC+3 that reached nine call sites
        across five modules -- so `find_meeting_time` merged UTC `events` for
        one attendee with Moscow wall-clock `availability_view` for another
        (PROJECT_STATUS.md #601/#602). `start`/`end` must be expressed in
        `tz_id` too: use `MailboxTimezone.to_wire_wallclock()`.
        """
        if tz_id is None:
            tz_id = self.mailbox_timezone().wire_id

        payload = [{
            "operationName": "GetSchedule",
            "variables": {
                "input": {
                    # The *requesting* user, not one of the queried mailboxes:
                    # the Scheduling Assistant sends its own address here. The
                    # fallback to an attendee is what this had to do while
                    # mailbox_address() didn't exist, and this tenant accepts
                    # it, so it stays as the degraded path rather than
                    # becoming an error.
                    "userId": self.mailbox_address() or (emails[0] if emails else ""),
                    "availabilityViewInterval": interval_minutes,
                    "schedules": emails,
                    "startTime": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S.000"), "timeZone": {"name": tz_id}},
                    "endTime": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S.000"), "timeZone": {"name": tz_id}},
                }
            },
            "query": self._GET_SCHEDULE_QUERY,
        }]

        data = self.request_substrate("/outlookgatewayb2/graphql", {}, payload)
        results = data if isinstance(data, list) else [data]
        top = results[0] if results else {}

        # A GraphQL operation that fails wholesale answers `"data": null` with the
        # reason in a sibling `errors` array. `top.get("data", {})` returns that
        # null rather than the default (the key *is* present), so chaining
        # `.get()` off it raised AttributeError: 'NoneType' object has no
        # attribute 'get' -- an opaque 500 out of find_meeting_time, with the
        # server's own explanation sitting unread in the response. Surfaced
        # while verifying the #601/#602 timezone fix, on a request for a mailbox
        # that does not exist. Raised as a RuntimeError naming the reason
        # because both callers already treat an exception here as "this
        # attendee/mailbox has no data I can use" and report it, which is the
        # right outcome -- what was wrong was that they could not say why.
        envelope = top.get("data")
        if not isinstance(envelope, dict):
            messages = [
                m.get("message", "") for m in (top.get("errors") or []) if isinstance(m, dict)
            ]
            raise RuntimeError(
                "GetSchedule returned no data: "
                + ("; ".join(m for m in messages if m) or "no error detail in the response")
            )

        schedules = (envelope.get("getSchedule") or {}).get("schedules") or []
        by_id = {s.get("scheduleId"): s for s in schedules}

        out = []
        for email in emails:
            s = by_id.get(email, {})
            events = []
            for item in s.get("scheduleItems", []) or []:
                sdt = self._parse_schedule_dt(item.get("startTime"))
                edt = self._parse_schedule_dt(item.get("endTime"))
                if not sdt or not edt:
                    continue
                events.append({
                    "start": sdt,
                    "end": edt,
                    "subject": item.get("subject", ""),
                    "status": item.get("status", ""),
                    "is_recurring": bool(item.get("isRecurring")),
                })
            out.append({
                "email": email,
                "availability_view": s.get("availabilityView", ""),
                "error": s.get("error"),
                "events": events,
            })
        return out

    # ------------------------------------------------------------------
    # Copilot (chat pane UI automation - no documented API exists)
    # ------------------------------------------------------------------

    def ask_copilot(
        self, prompt: str, *, item_id: str | None = None, item_kind: str = "email", timeout: float = 90
    ) -> dict:
        """Delegate a prompt to Copilot's chat pane in the modern Outlook web client.

        Unlike every other method here, this drives Copilot's UI directly
        (see BrowserSession.copilot_ask) instead of an EWS-style JSON action
        - Copilot has no documented API. Only available in "bearer" auth
        mode (BrowserSession.auth_mode); raises BearerModeRequiredError
        otherwise. CopilotUnavailableError propagates uncaught (rate-limit/
        capacity condition, not a session problem) - same retry-once-on-
        expiry idiom as request() otherwise.

        Args:
            prompt: Free-text question or instruction for Copilot.
            item_id: Optional item to ground the question against (opens
                that item first - best-effort; see _copilot_item_url).
            item_kind: "email" (default) or "event" - which deep-link shape
                to use for item_id.
            timeout: Seconds to wait for a complete response before
                returning a partial result instead of raising.

        Returns:
            {"status": "ok", "text": ...} or {"status": "timeout", "partial_text": ...}
        """
        nav_url = self._copilot_item_url(item_id, item_kind) if item_id else None
        fallback_url = self._copilot_launcher_fallback_url(item_kind) if item_id else None
        for attempt in range(2):
            try:
                return self.browser.copilot_ask(
                    prompt, nav_url=nav_url, launcher_fallback_url=fallback_url, timeout=timeout
                )
            except SessionExpiredError:
                if attempt == 0:
                    self._relogin_or_raise()
                else:
                    raise

        raise SessionExpiredError("Session expired. Call the login tool to log in again.")

    def _copilot_item_url(self, item_id: str, item_kind: str) -> str | None:
        """Deep link to ground Copilot on a specific item.

        Both shapes are **confirmed** against a live modern-Outlook session by
        discovery capture 20260911-112708-e917, which recorded exactly these
        navigations (`/mail/inbox/id/<urlencoded id>` when opening a message,
        `/calendar/item/<urlencoded id>` when opening an event). The one known
        limitation is the hardcoded `inbox` segment: the real URL carries the
        item's *folder*, so a message living elsewhere is not addressed
        precisely. BrowserSession.copilot_ask treats a failed navigation as
        non-fatal (falls back to an ungrounded ask), so that degrades rather
        than breaks the call.
        """
        origin = self.browser.bearer_origin
        if item_kind == "event":
            return f"{origin}/calendar/item/{quote(item_id, safe='')}"
        return f"{origin}/mail/inbox/id/{quote(item_id, safe='')}"

    def _copilot_launcher_fallback_url(self, item_kind: str) -> str | None:
        """Where to look for a Copilot launcher when the item page has none.

        A calendar *item* page carries no Copilot button - the discovery
        capture caught the user bouncing back to `/calendar/view/day` and
        reaching meeting prep from there, which also matches the original
        smoke failure ("Could not find a Copilot launch button"). Mail item
        pages do have a launcher, so they need no fallback.
        """
        if item_kind == "event":
            return f"{self.browser.bearer_origin}/calendar/view/day"
        return None
