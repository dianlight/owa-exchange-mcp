"""Pure resolution of *our own* mailbox SMTP address from session signals.

Why this module exists: `OWAClient.user_email` used to be written from the
username in the credential store — `login.py --setup` encrypted it, startup and
the `login` tool assigned it. Removing stored credentials (2026-09-10, 2.0.0b1,
commit f2f3c7b) deleted both writers and left the five readers behind, so from
that day on the field was permanently `""`: `get_meeting_contacts` returned
`"User email not available. Call the login tool first."` on every call (advice
that could not work — nothing writes it any more), `find_free_time` silently
took its non-recurrence-expanding fallback, and `get_schedule` sent an
attendee's address as the *requesting* user's id.

The address has to be *discovered* now, and discovery has three independent
signals of unequal trustworthiness. That ordering is the whole content of this
module, so it lives here rather than inline in the client:

1. `x-anchormailbox` (bearer mode only, already captured by
   `BrowserSession._async_capture_bearer_context`, costs no request). When it
   carries an `SMTP:` value this is authoritative — it is the address Exchange
   itself routes the session's requests on.
2. `GetOwaUserConfiguration` (one request, both backends). `PrimarySmtpAddress`
   / `UserEmailAddress` are the mailbox's own primary address by definition.
3. Claims in the session's Bearer JWT (bearer mode only, no request). Last
   because a UPN is *not* guaranteed to equal the primary SMTP address —
   hybrid/on-prem tenants routinely differ. Good enough to identify the
   mailbox, not good enough to prefer over 1 or 2, which is why the resolved
   `source` is reported: a self-exclusion that misses is diagnosable from it.

Everything here is pure: no Playwright, no transport, no I/O. Add new signals
to the tables below rather than to `owa_client.py`, and cover them in
`tests/unit/test_mailbox_identity.py` — same rule as `auth_errors.py` and
`profile_lock.py`, and for the same reason. An address is the sort of value
where a *wrong* answer is worse than none (`get_meeting_contacts` excludes
"self" by comparing against it), so every function here fails closed: anything
it cannot positively recognise as an SMTP address resolves to "".
"""

import base64
import binascii
import json
import re
from typing import NamedTuple

# Where a resolved address came from. Reported to callers (and recorded in
# tool output) because signal 3 below can legitimately disagree with the
# mailbox's primary address, and "which signal answered" is the only way to
# tell a confident answer from a plausible one after the fact.
SOURCE_ANCHOR_MAILBOX = "anchor_mailbox"
SOURCE_USER_CONFIGURATION = "owa_user_configuration"
SOURCE_BEARER_CLAIM = "bearer_claim"

# Why no address was resolved. These are the strings tools surface, so they
# are a contract, not prose.
UNRESOLVED_NO_SIGNALS = "no_identity_signals"
UNRESOLVED_NO_ADDRESS = "no_address_in_identity_signals"

# An SMTP address, deliberately stricter than RFC 5321: the domain must contain
# a dot and end in letters. That is what rejects the *other* thing
# x-anchormailbox carries — `PUID:1003bffd...@84df9e7f-e9f6-40af-b435-...`,
# whose "domain" is a tenant GUID (hyphens, no dot) and which would otherwise
# sail through a naive "has an @" check and be used as a mailbox address.
_SMTP_SHAPE = re.compile(r"^[^@\s;,:<>\"]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

# The only x-anchormailbox prefix that introduces an SMTP address. Observed
# forms on this tenant are `SMTP:user@example.com` and the PUID form above;
# an unrecognised prefix is rejected rather than guessed at, because every
# non-SMTP form is an opaque id that happens to contain "@".
_ANCHOR_SMTP_PREFIX = "smtp:"

# JWT claims that can carry the signed-in user's address, most-specific first.
# `smtp`/`email` are addresses by definition; `upn`/`unique_name`/
# `preferred_username` are sign-in names that usually but not always match the
# primary SMTP address (see the module docstring).
_JWT_ADDRESS_CLAIMS = ("smtp", "email", "upn", "preferred_username", "unique_name")

# Keys in a GetOwaUserConfiguration response that hold the mailbox's own
# address, in decreasing authority. Searched by name at any depth because the
# nesting differs between backends (classic puts them under
# UserConfiguration.SessionSettings; the modern backend's shape is not pinned
# down), and a name-keyed search survives a reshuffle that a hardcoded path
# would not. Every key here is self-referential in OWA's own config — that is
# the property that keeps a recursive walk from picking up somebody else's
# address out of, say, a delegate list.
_USER_CONFIG_ADDRESS_KEYS = (
    "PrimarySmtpAddress",
    "UserEmailAddress",
    "MailboxSmtpAddress",
    "SmtpAddress",
    "LogonEmailAddress",
    "PrimaryEmailAddress",
)

# Depth cap on that walk. OWA's user configuration is a wide, shallow blob;
# anything deeper than this is not the mailbox's own identity, and the cap
# means a backend that starts returning a self-referential structure degrades
# into "not found" instead of hanging.
_MAX_WALK_DEPTH = 8


class MailboxAddress(NamedTuple):
    """A resolved own-mailbox address, or a resolved *failure* to find one.

    `address` is "" exactly when resolution failed, and then `reason` says
    which failure it was — `UNRESOLVED_NO_SIGNALS` (nothing to look at, e.g.
    classic OWA before any request has been made) versus
    `UNRESOLVED_NO_ADDRESS` (signals were present and carried no address,
    which is the one worth reporting to a human). `source` names the signal
    that answered; see the module docstring for why callers keep it.

    `detail` is free text about *why* a signal didn't answer — the exception a
    swallowed probe raised, typically. It exists because diagnosing the
    2026-09-16 cold-start miss took three live probes to recover a single
    `TimeoutError` that `_user_configuration_or_none`'s `except Exception` had
    thrown away, and the wrong mechanism was written down twice in the
    meantime. Never parse it; `reason` is the contract.
    """

    address: str
    source: str = ""
    reason: str = ""
    detail: str = ""


def looks_like_smtp_address(value: str) -> bool:
    """True if `value` is recognisably an SMTP address (see `_SMTP_SHAPE`)."""
    return bool(_SMTP_SHAPE.match((value or "").strip()))


def normalize_address(value: str) -> str:
    """Strip an address of the packaging it arrives in, or return "".

    Handles `<user@example.com>`, stray whitespace and a trailing `;` from
    header-ish values. Case is *preserved*: the server's own casing is the
    best casing to send back to it, and every comparison in this codebase
    already lowercases at the point of comparison.
    """
    text = (value or "").strip().strip(";").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return text if looks_like_smtp_address(text) else ""


def address_from_anchor_mailbox(header_value: str) -> str:
    """Extract the address from an `x-anchormailbox` header value, or "".

    Accepts a bare address or an explicit `SMTP:` prefix. Any other prefix is
    an opaque identifier (`PUID:`, and whatever else a tenant sends) and is
    rejected — see `_ANCHOR_SMTP_PREFIX`.
    """
    text = (header_value or "").strip()
    if not text:
        return ""
    if ":" in text:
        prefix, _, rest = text.partition(":")
        if f"{prefix.lower()}:" != _ANCHOR_SMTP_PREFIX:
            return ""
        text = rest
    return normalize_address(text)


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT's payload claims without verifying it, or return {}.

    Verification is deliberately absent and harmless here: the token came out
    of our *own* authenticated page's request headers, and the only thing read
    from it is an address used to identify the session's own mailbox. Accepts
    a raw token or a full `Bearer <token>` header value.
    """
    text = (token or "").strip()
    if text.lower().startswith("bearer "):
        text = text.split(" ", 1)[1].strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def address_from_jwt_claims(claims: dict) -> tuple[str, str]:
    """Pick an address out of decoded JWT claims. Returns (address, claim_name)."""
    if not isinstance(claims, dict):
        return "", ""
    for claim in _JWT_ADDRESS_CLAIMS:
        address = normalize_address(claims.get(claim) if isinstance(claims.get(claim), str) else "")
        if address:
            return address, claim
    return "", ""


def address_from_user_configuration(data) -> tuple[str, str]:
    """Pick the mailbox's own address out of a GetOwaUserConfiguration response.

    Returns (address, path) where `path` is the dotted location it was found
    at, so a wrong pick is diagnosable from the tool output that reports it.
    Searches `_USER_CONFIG_ADDRESS_KEYS` in priority order — one full walk per
    key rather than one walk taking the first key it bumps into, because
    "shallower in the response" is not the same as "more authoritative".
    """
    for key in _USER_CONFIG_ADDRESS_KEYS:
        address, path = _find_key(data, key, "", 0)
        if address:
            return address, path
    return "", ""


def _find_key(node, wanted: str, path: str, depth: int) -> tuple[str, str]:
    """Depth-first search for `wanted`, returning (normalized address, path)."""
    if depth > _MAX_WALK_DEPTH:
        return "", ""
    if isinstance(node, dict):
        raw = node.get(wanted)
        if isinstance(raw, str):
            address = normalize_address(raw)
            if address:
                return address, f"{path}.{wanted}" if path else wanted
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                found, found_path = _find_key(value, wanted, f"{path}.{key}" if path else key, depth + 1)
                if found:
                    return found, found_path
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, (dict, list)):
                found, found_path = _find_key(value, wanted, f"{path}[{index}]", depth + 1)
                if found:
                    return found, found_path
    return "", ""


def resolve_mailbox_address(
    *,
    anchor_mailbox: str = "",
    user_configuration=None,
    bearer_token: str = "",
) -> MailboxAddress:
    """Resolve our own mailbox address from whichever signals are available.

    Priority is the module docstring's 1-2-3, and it is the reason this is one
    function rather than three calls at the call site: the caller shouldn't be
    able to accidentally prefer a UPN claim over a header that Exchange itself
    routed on. Never raises; a signal it can't read is a signal that didn't
    answer.
    """
    saw_signal = bool((anchor_mailbox or "").strip()) or user_configuration is not None

    address = address_from_anchor_mailbox(anchor_mailbox)
    if address:
        return MailboxAddress(address, SOURCE_ANCHOR_MAILBOX)

    if user_configuration is not None:
        address, path = address_from_user_configuration(user_configuration)
        if address:
            return MailboxAddress(address, f"{SOURCE_USER_CONFIGURATION}:{path}")

    claims = decode_jwt_claims(bearer_token)
    if claims:
        saw_signal = True
        address, claim = address_from_jwt_claims(claims)
        if address:
            return MailboxAddress(address, f"{SOURCE_BEARER_CLAIM}:{claim}")

    return MailboxAddress(
        "", "", UNRESOLVED_NO_ADDRESS if saw_signal else UNRESOLVED_NO_SIGNALS
    )


def is_retryable(resolved: MailboxAddress) -> bool:
    """True when a failed resolution says "nothing had answered *yet*".

    `UNRESOLVED_NO_ADDRESS` is a property of the backend — signals were there
    and carried no address — so it will not change mid-process and caching it
    is right. `UNRESOLVED_NO_SIGNALS` is a statement about *when* we asked, so
    it may.

    Naming the rule here rather than comparing reason strings in the client is
    the point: this is the distinction the 2026-09-16 cold-start bug turned on,
    and a caller open-coding `!= UNRESOLVED_NO_SIGNALS` is how it comes back.
    """
    return not resolved.address and resolved.reason == UNRESOLVED_NO_SIGNALS


def signal_state(hints: dict | None) -> str:
    """A fingerprint of *which* identity signals this session currently holds.

    Two states that compare equal mean "re-asking would read exactly the same
    inputs", which is what makes it safe to serve a cached failure rather than
    spend another request. It is the bound on the retry that `is_retryable()`
    allows, and it is a real bound rather than a counter because the failure it
    guards against is *sticky per session*: measured 2026-09-16, four cold
    processes against one profile resolved twice on the first call and, in the
    two that did not, failed all three further attempts — 0 for 6. Retrying
    inside an unchanged session buys nothing and costs ~30s a call.

    Presence only, never content: a Bearer token that was merely *refreshed*
    names the same mailbox, so comparing tokens would force a pointless
    re-probe every hour, while an account switch is handled by
    `forget_mailbox_identity()` instead. `auth_mode` is included because it is
    the one field that can move without a hint appearing, and a re-probe in
    that state is exactly the one worth paying for.
    """
    hints = hints or {}
    return "|".join((
        str(hints.get("auth_mode", "")),
        "anchor" if str(hints.get("anchor_mailbox", "")).strip() else "-",
        "token" if str(hints.get("bearer_token", "")).strip() else "-",
    ))
