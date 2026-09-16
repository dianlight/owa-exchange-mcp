"""Unit test: resolving *our own* mailbox address from session signals.

Unlike tests/smoke/, this needs no live mailbox, no browser and no
EXCHANGE_OWA_URL -- everything under test is pure logic.

Why this exists: `OWAClient.user_email` sat at "" for six days (2026-09-10 to
2026-09-16) after the credential store that used to write it was removed, and
the five readers left behind failed in three different ways -- one tool erroring
on every call, one silently taking a fallback that reports busy time as free,
and a request sending an attendee's address as the requesting user's id. So the
checks here pin down the two properties that make the replacement safe to rely
on:

1. It fails *closed*. An identifier that merely contains "@" -- most
   importantly the `PUID:<hex>@<tenant guid>` form real x-anchormailbox headers
   carry -- must resolve to "" rather than be used as a mailbox address. A
   wrong address is worse than none here: get_meeting_contacts excludes "self"
   by comparing against it, so a wrong one silently keeps the user in their own
   contact ranking, while an empty one is reported.
2. The signal priority holds. A sign-in-name claim (`upn`) must never win over
   an authoritative address (`SMTP:` anchor header, or the config's
   PrimarySmtpAddress), because a UPN is not guaranteed to equal the primary
   SMTP address on hybrid tenants.

Run standalone:
    python -m tests.unit.test_mailbox_identity
"""

import base64
import json
import os
import sys

from exchange_mcp import mailbox_identity as mi
from exchange_mcp.owa_client import OWAClient

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


def jwt(claims: dict) -> str:
    """Build an unsigned JWT carrying `claims` (payload is all that's read)."""
    def seg(obj) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
        return raw.rstrip("=")  # real tokens are unpadded; the decoder must cope
    return f"{seg({'alg': 'RS256', 'typ': 'JWT'})}.{seg(claims)}.signature-not-checked"


# The PUID shape a modern-Outlook x-anchormailbox really carries when it isn't
# an SMTP address. It has an "@" and it is not a mailbox address.
PUID_ANCHOR = "PUID:1003BFFD9A1B2C3D@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa"


def test_smtp_shape() -> None:
    print("An SMTP address is recognised; an opaque id containing '@' is not")
    for good in ("user@example.com", "First.Last@mail.corp.example.co.uk", "a@b.io"):
        check(f"accepts {good}", mi.looks_like_smtp_address(good))
    for bad in (
        "1003BFFD9A1B2C3D@84df9e7f-e9f6-40af-b435-aaaaaaaaaaaa",  # PUID, GUID "domain"
        "user@localhost",       # no dot in the domain
        "user@example.",        # empty TLD
        "user@example.c",       # 1-char TLD
        "no-at-sign.example.com",
        "two@at@example.com",
        "SMTP:user@example.com",  # prefix must be stripped first, not accepted here
        "user @example.com",
        "",
        None,
    ):
        check(f"rejects {bad!r}", not mi.looks_like_smtp_address(bad))


def test_normalize() -> None:
    print("Packaging is stripped; case is preserved")
    check("angle brackets", mi.normalize_address("<User@Example.com>") == "User@Example.com")
    check("whitespace + semicolon", mi.normalize_address("  user@example.com; ") == "user@example.com")
    check(
        "case preserved (the server's casing is the best casing to send back)",
        mi.normalize_address("First.Last@Example.COM") == "First.Last@Example.COM",
    )
    check("garbage -> empty", mi.normalize_address("not an address") == "")


def test_anchor_mailbox() -> None:
    print("x-anchormailbox: SMTP: prefix or bare address only")
    check(
        "SMTP: prefix (case-insensitive)",
        mi.address_from_anchor_mailbox("SMTP:user@example.com") == "user@example.com",
    )
    check(
        "smtp: lowercase prefix",
        mi.address_from_anchor_mailbox("smtp:user@example.com") == "user@example.com",
    )
    check(
        "bare address",
        mi.address_from_anchor_mailbox("user@example.com") == "user@example.com",
    )
    check(
        "PUID form is rejected, not mistaken for an address",
        mi.address_from_anchor_mailbox(PUID_ANCHOR) == "",
        mi.address_from_anchor_mailbox(PUID_ANCHOR),
    )
    check(
        "an unknown prefix is rejected rather than guessed at",
        mi.address_from_anchor_mailbox("OID:user@example.com") == "",
    )
    check("empty header", mi.address_from_anchor_mailbox("") == "")


def test_jwt_claims() -> None:
    print("Bearer JWT claims: decoded unverified, most-specific claim wins")
    token = jwt({"upn": "logon@example.com", "smtp": "primary@example.com", "exp": 123})
    claims = mi.decode_jwt_claims(token)
    check("decodes unpadded payload", claims.get("smtp") == "primary@example.com", repr(claims))
    check(
        "full 'Bearer <token>' header value is accepted",
        mi.decode_jwt_claims(f"Bearer {token}").get("smtp") == "primary@example.com",
    )
    address, claim = mi.address_from_jwt_claims(claims)
    check("smtp beats upn", (address, claim) == ("primary@example.com", "smtp"), f"{address} via {claim}")

    upn_only = mi.decode_jwt_claims(jwt({"upn": "logon@example.com"}))
    check("upn used when nothing better", mi.address_from_jwt_claims(upn_only)[0] == "logon@example.com")

    # A garbage token must not raise: this runs on the auth path.
    for junk in ("", "not.a.jwt", "one-part", "a.!!!not-base64!!!.c", None):
        check(f"garbage token {junk!r} -> {{}}", mi.decode_jwt_claims(junk) == {})
    check("non-dict payload -> {}", mi.decode_jwt_claims(jwt(["not", "a", "dict"])) == {})


# A GetOwaUserConfiguration response in the classic OWA nesting. The extra
# addresses are the reason the walk is keyed on *self-referential* key names:
# an unrelated address elsewhere in the config must not be picked up.
USER_CONFIG = {
    "Header": {"ServerVersionInfo": {"MajorVersion": 15}},
    "Body": {
        "__type": "GetOwaUserConfigurationResponse:#Exchange",
        "UserConfiguration": {
            "UserOptions": {"TimeZone": "W. Europe Standard Time"},
            "SessionSettings": {
                "UserDisplayName": "First Last",
                "UserEmailAddress": "first.last@example.com",
                "LogonEmailAddress": "flast@example.com",
            },
            "PolicySettings": {"OwnerEmailAddress": "someone.else@example.com"},
        },
    },
}


def test_user_configuration() -> None:
    print("GetOwaUserConfiguration: self-referential keys, in priority order")
    address, path = mi.address_from_user_configuration(USER_CONFIG)
    check(
        "UserEmailAddress found at depth, path reported",
        (address, path) == ("first.last@example.com", "Body.UserConfiguration.SessionSettings.UserEmailAddress"),
        f"{address} at {path}",
    )

    with_primary = json.loads(json.dumps(USER_CONFIG))
    with_primary["Body"]["UserConfiguration"]["SessionSettings"]["PrimarySmtpAddress"] = "primary@example.com"
    check(
        "PrimarySmtpAddress outranks UserEmailAddress",
        mi.address_from_user_configuration(with_primary)[0] == "primary@example.com",
    )

    check(
        "a key that isn't about us is never read",
        "someone.else" not in mi.address_from_user_configuration(USER_CONFIG)[0],
    )
    check("no addresses at all -> empty", mi.address_from_user_configuration({"Body": {}}) == ("", ""))
    check("None response -> empty", mi.address_from_user_configuration(None) == ("", ""))

    # A response that nests itself must terminate rather than recurse forever.
    looping: dict = {"Body": {}}
    looping["Body"]["self"] = looping
    check("self-nesting response terminates", mi.address_from_user_configuration(looping) == ("", ""))


def test_resolution_priority() -> None:
    print("Priority: SMTP anchor header > user configuration > token claim")
    anchor_wins = mi.resolve_mailbox_address(
        anchor_mailbox="SMTP:anchor@example.com",
        user_configuration=USER_CONFIG,
        bearer_token=jwt({"upn": "logon@example.com"}),
    )
    check(
        "anchor header wins",
        (anchor_wins.address, anchor_wins.source) == ("anchor@example.com", mi.SOURCE_ANCHOR_MAILBOX),
        repr(anchor_wins),
    )

    config_wins = mi.resolve_mailbox_address(
        anchor_mailbox=PUID_ANCHOR,
        user_configuration=USER_CONFIG,
        bearer_token=jwt({"upn": "logon@example.com"}),
    )
    check(
        "a PUID anchor falls through to the configuration, not to the claim",
        config_wins.address == "first.last@example.com",
        repr(config_wins),
    )
    check(
        "configuration source carries the path it was found at",
        config_wins.source.startswith(f"{mi.SOURCE_USER_CONFIGURATION}:"),
        config_wins.source,
    )

    claim_only = mi.resolve_mailbox_address(bearer_token=jwt({"upn": "logon@example.com"}))
    check(
        "claim is used when it's all there is, and says so",
        (claim_only.address, claim_only.source) == ("logon@example.com", f"{mi.SOURCE_BEARER_CLAIM}:upn"),
        repr(claim_only),
    )


def test_unresolved_reasons() -> None:
    print("A failure is reported as which failure it was")
    nothing = mi.resolve_mailbox_address()
    check(
        "no signals at all (classic OWA before any request)",
        (nothing.address, nothing.reason) == ("", mi.UNRESOLVED_NO_SIGNALS),
        repr(nothing),
    )

    present_but_useless = mi.resolve_mailbox_address(
        anchor_mailbox=PUID_ANCHOR,
        user_configuration={"Body": {"UserConfiguration": {"UserOptions": {}}}},
        bearer_token=jwt({"oid": "9c1e-not-an-address"}),
    )
    check(
        "signals present but carrying no address",
        (present_but_useless.address, present_but_useless.reason)
        == ("", mi.UNRESOLVED_NO_ADDRESS),
        repr(present_but_useless),
    )
    check("no source when unresolved", present_but_useless.source == "")


def test_source_and_reason_strings_are_stable() -> None:
    print("Sources and reasons are the literal strings tools surface")
    expected = {
        "SOURCE_ANCHOR_MAILBOX": "anchor_mailbox",
        "SOURCE_USER_CONFIGURATION": "owa_user_configuration",
        "SOURCE_BEARER_CLAIM": "bearer_claim",
        "UNRESOLVED_NO_SIGNALS": "no_identity_signals",
        "UNRESOLVED_NO_ADDRESS": "no_address_in_identity_signals",
    }
    for name, literal in expected.items():
        check(f"{name} == {literal!r}", getattr(mi, name) == literal, getattr(mi, name))


# ------------------------------------------------------------------
# OWAClient.resolve_own_mailbox(): caching, degradation, request count
#
# Same fake-transport approach as tests/unit/test_folder_resolution.py — a
# subclassed client answers GetOwaUserConfiguration from memory and counts the
# calls, so "at most one request ever" is assertable rather than aspirational.
# ------------------------------------------------------------------


class FakeBrowser:
    """Just the surface OWAClient touches, plus mutable identity hints.

    `hints_after_request` models the ordering that actually happens on a cold
    session: the bearer token (and with it x-anchormailbox) is captured *by*
    the first request, so hints that were empty before it can be populated
    after — which is why resolve_own_mailbox re-reads them.
    """

    def __init__(self, hints: dict | None = None, hints_after_request: dict | None = None):
        self.owa_url = "https://owa.example.com"
        self.profile_dir = "/tmp/profile"
        self.auth_mode = "canary"
        self._hints = hints or {}
        self._hints_after_request = hints_after_request

    def identity_hints(self) -> dict:
        return dict(self._hints)

    def note_request(self) -> None:
        if self._hints_after_request is not None:
            self._hints = self._hints_after_request

    def arrive_late(self, hints: dict) -> None:
        """Signals appearing later in the process, e.g. after a substrate call.

        The case `signal_state()` exists for: nothing about the *session*
        changed except that a Bearer context now exists, and that is precisely
        when a cached "nothing answered yet" is worth re-asking.
        """
        self._hints = hints


class FakeClient(OWAClient):
    """OWAClient whose GetOwaUserConfiguration is served from memory."""

    def __init__(self, *, hints: dict | None = None, hints_after_request: dict | None = None,
                 config=None, fail_with: Exception | None = None):
        super().__init__(FakeBrowser(hints, hints_after_request))
        self.config = config
        self.fail_with = fail_with
        self.calls: list[str] = []

    def request(self, action: str, payload: dict, *, timeout: int = 30) -> dict:
        self.calls.append(action)
        self.browser.note_request()
        if self.fail_with is not None:
            raise self.fail_with
        if self.config is None:
            raise AssertionError(f"unexpected request {action}")
        return self.config


def test_client_anchor_hint_costs_no_request() -> None:
    print("An SMTP anchor header answers with zero requests")
    client = FakeClient(hints={"anchor_mailbox": "SMTP:user@example.com", "bearer_token": ""})
    check("address resolved", client.mailbox_address() == "user@example.com")
    check("no request was made", client.calls == [], repr(client.calls))


def test_client_falls_back_to_user_configuration() -> None:
    print("No usable hint -> exactly one GetOwaUserConfiguration, then cached")
    client = FakeClient(hints={"anchor_mailbox": PUID_ANCHOR}, config=USER_CONFIG)
    first = client.resolve_own_mailbox()
    check(
        "address from the configuration",
        first.address == "first.last@example.com",
        repr(first),
    )
    check("one request", client.calls == ["GetOwaUserConfiguration"], repr(client.calls))

    for _ in range(5):
        client.mailbox_address()
    check("repeat reads are cached, not re-requested", len(client.calls) == 1, repr(client.calls))

    client.forget_mailbox_identity()
    check("forget_mailbox_identity() re-resolves", client.mailbox_address() == "first.last@example.com")
    check("...with a fresh request", len(client.calls) == 2, repr(client.calls))


def test_client_rereads_hints_after_the_request() -> None:
    print("A bearer capture triggered *by* the probe request is still picked up")
    client = FakeClient(
        hints={},
        hints_after_request={"anchor_mailbox": "SMTP:late@example.com", "bearer_token": ""},
        config={"Body": {}},  # config itself carries nothing usable
    )
    resolved = client.resolve_own_mailbox()
    check(
        "hint captured during the request is used",
        (resolved.address, resolved.source) == ("late@example.com", mi.SOURCE_ANCHOR_MAILBOX),
        repr(resolved),
    )


def test_client_degrades_and_never_raises() -> None:
    print("A backend that won't say who we are degrades; it does not take tools down")
    client = FakeClient(hints={}, fail_with=RuntimeError("GetOwaUserConfiguration: HTTP 500"))
    resolved = client.resolve_own_mailbox()
    check("empty address", resolved.address == "", repr(resolved))
    check("reason reported", resolved.reason == mi.UNRESOLVED_NO_SIGNALS, resolved.reason)
    check("one attempt", client.calls == ["GetOwaUserConfiguration"], repr(client.calls))

    for _ in range(5):
        client.mailbox_address()
    check(
        "a failure is cached too: no request per call for the life of the process",
        len(client.calls) == 1,
        repr(client.calls),
    )


def test_signal_state_and_is_retryable() -> None:
    print("The two rules the retry is built on, named in the pure module")
    cold = {"auth_mode": "canary", "anchor_mailbox": "", "bearer_token": ""}
    warm = {"auth_mode": "bearer", "anchor_mailbox": PUID_ANCHOR, "bearer_token": "Bearer abc"}
    check("cold != warm", mi.signal_state(cold) != mi.signal_state(warm))
    check("None is a state", mi.signal_state(None) == mi.signal_state({}))
    # A refreshed token names the same mailbox: it must not force a re-probe.
    check("a rotated token is the same state",
          mi.signal_state(warm) == mi.signal_state(dict(warm, bearer_token="Bearer zzz")))
    # auth_mode can move without a hint appearing, and that *is* worth a look.
    check("auth_mode counts",
          mi.signal_state(cold) != mi.signal_state(dict(cold, auth_mode="bearer")))

    check("no signals -> retryable",
          mi.is_retryable(mi.MailboxAddress("", "", mi.UNRESOLVED_NO_SIGNALS)))
    check("signals without an address -> not",
          not mi.is_retryable(mi.MailboxAddress("", "", mi.UNRESOLVED_NO_ADDRESS)))
    check("a resolved address -> never",
          not mi.is_retryable(mi.MailboxAddress("a@b.com", mi.SOURCE_ANCHOR_MAILBOX)))


def test_no_signals_is_retried_but_no_address_is_cached() -> None:
    print("'Nothing could answer yet' retries; 'this backend has no address' caches")
    # Which failure it was decides whether caching it is right, and getting that
    # wrong is not cosmetic. None of the three signals exists until a bearer
    # token has been captured, and this codebase mints one lazily, so on a
    # freshly started server the first availability call can lose the race.
    # Caching that miss pinned find_free_time to the calendar-folder scan --
    # which reports booked recurring time as *free* -- for the life of the
    # process, and an stdio server is a fresh process per client session, so
    # that was the common path rather than an edge case (PROJECT_STATUS.md §4,
    # found live 2026-09-16).
    cold = FakeClient(hints={}, fail_with=RuntimeError("Timeout 8000ms exceeded"))
    first = cold.resolve_own_mailbox()
    check("cold start reports no signals", first.reason == mi.UNRESOLVED_NO_SIGNALS, first.reason)
    check("and says why, rather than swallowing it", "Timeout" in first.detail, repr(first.detail))
    for _ in range(3):
        cold.mailbox_address()
    check(
        # The retry is *bounded by the signal state*, not unconditional. An
        # unconditional one re-probes on every availability call while
        # unresolved, and the flake this guards against is sticky per session:
        # measured 2026-09-16, four cold processes resolved twice on the first
        # call and, in the two that didn't, failed all three further attempts --
        # 0 for 6, at ~30s each. See mailbox_identity.signal_state().
        "while the session's signals are unchanged, it is not re-probed",
        len(cold.calls) == 1,
        repr(cold.calls),
    )

    # ...and the moment a signal turns up, the retry picks it up rather than
    # returning a stale empty. This is the behaviour the retry exists *for*.
    cold.browser.arrive_late({"auth_mode": "bearer", "anchor_mailbox": "SMTP:warm@example.com",
                              "bearer_token": "Bearer abc"})
    healed = cold.resolve_own_mailbox()
    check("a changed signal state re-resolves", healed.address == "warm@example.com", repr(healed))
    check("...for free, from the hint", len(cold.calls) == 1, repr(cold.calls))

    warmed = FakeClient(
        hints={},
        hints_after_request={"anchor_mailbox": "SMTP:warm@example.com", "bearer_token": ""},
        config={"Body": {}},
    )
    check("a hint captured during the probe still answers",
          warmed.resolve_own_mailbox().address == "warm@example.com")

    # The other failure caches, and that asymmetry is the point: a signal *was*
    # present and carried no address, which is a backend that will not start
    # answering mid-process. Re-probing it would add a request to every
    # availability call forever.
    no_address = FakeClient(hints={"anchor_mailbox": PUID_ANCHOR}, config={"Body": {"UserOptions": {}}})
    resolved = no_address.resolve_own_mailbox()
    check(
        "a signal with no address in it",
        resolved.reason == mi.UNRESOLVED_NO_ADDRESS,
        repr(resolved),
    )
    for _ in range(5):
        no_address.mailbox_address()
    check(
        "...is cached: one request for the life of the process",
        len(no_address.calls) == 1,
        repr(no_address.calls),
    )


def test_signal_state_and_is_retryable() -> None:
    print("The two rules the retry is built on, named in the pure module")
    cold = {"auth_mode": "canary", "anchor_mailbox": "", "bearer_token": ""}
    warm = {"auth_mode": "bearer", "anchor_mailbox": PUID_ANCHOR, "bearer_token": "Bearer abc"}
    check("cold != warm", mi.signal_state(cold) != mi.signal_state(warm))
    check("None is a state", mi.signal_state(None) == mi.signal_state({}))
    # A refreshed token names the same mailbox: it must not force a re-probe.
    check("a rotated token is the same state",
          mi.signal_state(warm) == mi.signal_state(dict(warm, bearer_token="Bearer zzz")))
    # auth_mode can move without a hint appearing, and that *is* worth a look.
    check("auth_mode counts",
          mi.signal_state(cold) != mi.signal_state(dict(cold, auth_mode="bearer")))

    check("no signals -> retryable",
          mi.is_retryable(mi.MailboxAddress("", "", mi.UNRESOLVED_NO_SIGNALS)))
    check("signals without an address -> not",
          not mi.is_retryable(mi.MailboxAddress("", "", mi.UNRESOLVED_NO_ADDRESS)))
    check("a resolved address -> never",
          not mi.is_retryable(mi.MailboxAddress("a@b.com", mi.SOURCE_ANCHOR_MAILBOX)))


def test_configuration_outranks_a_upn_claim_on_the_client() -> None:
    print("A bearer upn claim must not pre-empt the configuration (live regression, 2026-09-16)")
    # test_resolution_priority already pins this *inside*
    # resolve_mailbox_address(). What it cannot see is the sequencing in
    # OWAClient.resolve_own_mailbox(), which used to hand the bearer token to a
    # first pass made without the configuration -- so the claim answered and the
    # higher-authority configuration was never fetched. Every bearer-mode session
    # took that path: it surfaced live as get_meeting_contacts resolving from
    # `bearer_claim:upn` and reporting the "you may appear in your own ranking"
    # warning, on a mailbox whose configuration answers.
    upn_token = jwt({"upn": "sign.in.name@example.com"})
    client = FakeClient(
        hints={"anchor_mailbox": PUID_ANCHOR, "bearer_token": upn_token},
        config=USER_CONFIG,
    )
    resolved = client.resolve_own_mailbox()
    check(
        "the configuration's address wins over the claim",
        resolved.address == "first.last@example.com",
        repr(resolved),
    )
    check(
        "...and the source says so",
        resolved.source.startswith(mi.SOURCE_USER_CONFIGURATION),
        resolved.source,
    )

    # The claim is still the last resort, not dead code: with no configuration to
    # outrank it, it must answer rather than degrade to "".
    claim_only = FakeClient(hints={"bearer_token": upn_token}, config={"Body": {}})
    fallback = claim_only.resolve_own_mailbox()
    check(
        "a claim still answers when nothing outranks it",
        fallback.address == "sign.in.name@example.com",
        repr(fallback),
    )
    check(
        "...reported as a claim",
        fallback.source.startswith(mi.SOURCE_BEARER_CLAIM),
        fallback.source,
    )


def test_address_and_timezone_share_one_request() -> None:
    print("The address and the timezone are answered by one GetOwaUserConfiguration")
    # An override would skip the timezone probe entirely, so a real one in the
    # developer's shell would make every request count below meaningless.
    saved = os.environ.pop("EXCHANGE_TIMEZONE", None)
    try:
        client = FakeClient(hints={"anchor_mailbox": PUID_ANCHOR}, config=USER_CONFIG)
        check(
            "timezone resolved",
            client.mailbox_timezone() == "W. Europe Standard Time",
            client.mailbox_timezone(),
        )
        check(
            "address resolved",
            client.mailbox_address() == "first.last@example.com",
            client.mailbox_address(),
        )
        check("one request served both", client.calls == ["GetOwaUserConfiguration"], repr(client.calls))

        # Order-independent: whichever question is asked first pays for it.
        reversed_order = FakeClient(hints={"anchor_mailbox": PUID_ANCHOR}, config=USER_CONFIG)
        reversed_order.mailbox_address()
        reversed_order.mailbox_timezone()
        check(
            "...whichever of the two is asked first",
            reversed_order.calls == ["GetOwaUserConfiguration"],
            repr(reversed_order.calls),
        )

        # An account switch has to drop the shared blob, not just the address.
        # Dropping the address alone would re-resolve it out of the *previous*
        # account's cached configuration, so a stale address would come back
        # looking freshly probed -- which a value check cannot see, only a
        # request count can.
        client.forget_mailbox_identity()
        check("address re-resolves after an account switch",
              client.mailbox_address() == "first.last@example.com")
        check(
            "...from a fresh request, not the old account's cached blob",
            len(client.calls) == 2,
            repr(client.calls),
        )
        check("...and so does the timezone", client.mailbox_timezone() == "W. Europe Standard Time")
        check("...still sharing that one fresh request", len(client.calls) == 2, repr(client.calls))
    finally:
        if saved is not None:
            os.environ["EXCHANGE_TIMEZONE"] = saved


def main() -> bool:
    for test in (
        test_smtp_shape,
        test_normalize,
        test_anchor_mailbox,
        test_jwt_claims,
        test_user_configuration,
        test_resolution_priority,
        test_unresolved_reasons,
        test_source_and_reason_strings_are_stable,
        test_client_anchor_hint_costs_no_request,
        test_client_falls_back_to_user_configuration,
        test_client_rereads_hints_after_the_request,
        test_client_degrades_and_never_raises,
        test_no_signals_is_retried_but_no_address_is_cached,
        test_signal_state_and_is_retryable,
        test_configuration_outranks_a_upn_claim_on_the_client,
        test_address_and_timezone_share_one_request,
    ):
        test()
    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"  - {failure}")
        return False
    print("All checks passed.")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
