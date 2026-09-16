"""Unit test: which timezone id this server puts on the wire (issue #8).

Needs no live mailbox, no browser and no EXCHANGE_OWA_URL. The transport half
(`OWAClient.mailbox_timezone_detail`) is exercised against a fake `request()`
that records the actions asked for, so the caching, probe order and
degrade-don't-raise behaviour are covered without a mailbox.

The asymmetry to keep in mind while reading the checks -- it is the reason
issue #8 survived as long as it did:

- Sending the *wrong* zone is silent. `Russian Standard Time` on a UTC+3
  mailbox is correct, on any other mailbox it is a reminder that fires three
  hours early and a tool that reports success either way. Nothing in any
  response said which zone had been applied.
- So the checks below lean hard on two things beyond "the right value comes
  out": that the fallback is a zone whose wrongness is *self-evident* (UTC,
  never a regional zone), and that a shape nobody recognises reads as
  "discovery found nothing" rather than as a guess.

Run standalone:
    python -m tests.unit.test_mailbox_timezone
"""

import sys

from exchange_mcp import mailbox_timezone as mtz
from exchange_mcp.owa_client import OWAClient
from exchange_mcp.browser_session import SessionExpiredError

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


# ------------------------------------------------------------------
# Shape guard
# ------------------------------------------------------------------


def test_looks_like_timezone_id() -> None:
    print("What can be a timezone id at all")
    for good in (
        "UTC",
        "W. Europe Standard Time",
        "Russian Standard Time",
        "Pacific Standard Time",
        "Europe/Rome",
        "America/Argentina/Buenos_Aires",
        "(UTC+01:00) Amsterdam",
        "  W. Europe Standard Time  ",
    ):
        check(f"accepts {good!r}", mtz.looks_like_timezone_id(good))

    for bad, why in (
        ("", "empty"),
        ("   ", "blank"),
        ("none", "sentinel"),
        ("None", "sentinel, other case"),
        ("unspecified", "sentinel"),
        ('{"Id": "UTC"}', "a JSON fragment, not an id"),
        ("Time zone is not set for this mailbox, please\nconfigure it", "a sentence with a newline"),
        ("X" * 81, "longer than any real id"),
        (None, "not a string"),
        (42, "not a string"),
        (["UTC"], "not a string"),
    ):
        check(f"rejects {why}", not mtz.looks_like_timezone_id(bad), repr(bad))


# ------------------------------------------------------------------
# Precedence
# ------------------------------------------------------------------


def test_precedence() -> None:
    print("env > mailbox > UTC")
    both = mtz.choose("W. Europe Standard Time", "Europe/Rome")
    check("the env override wins over a discovered zone",
          both.timezone_id == "W. Europe Standard Time" and both.source == mtz.SOURCE_ENV,
          repr(both))

    discovered = mtz.choose(None, "Europe/Rome")
    check("a discovered zone is used when there is no override",
          discovered.timezone_id == "Europe/Rome" and discovered.source == mtz.SOURCE_MAILBOX,
          repr(discovered))

    neither = mtz.choose(None, None)
    check("neither source falls back", neither.source == mtz.SOURCE_FALLBACK, repr(neither))
    check("the fallback is UTC", neither.timezone_id == "UTC", neither.timezone_id)
    check("the fallback explains itself", bool(neither.detail), repr(neither.detail))
    check("is_fallback flags it", neither.is_fallback and not discovered.is_fallback)

    # The whole point of issue #8: a regional fallback is *plausible* and
    # therefore hides. UTC is wrong in a way somebody notices.
    check("the fallback is not a regional zone",
          mtz.FALLBACK_TIMEZONE_ID == "UTC"
          and mtz.FALLBACK_TIMEZONE_ID != mtz.LEGACY_HARDCODED_TIMEZONE_ID)

    junk = mtz.choose('{"Id": "UTC"}', "  ")
    check("unusable values on both sides still land on the fallback",
          junk.source == mtz.SOURCE_FALLBACK, repr(junk))

    custom = mtz.choose(None, None, detail="GetOwaUserConfiguration: HTTP 500")
    check("a caller-supplied detail is preserved",
          custom.detail == "GetOwaUserConfiguration: HTTP 500", repr(custom.detail))


def test_env_reading() -> None:
    print("Reading EXCHANGE_TIMEZONE")
    check("reads and strips the variable",
          mtz.timezone_id_from_env({mtz.ENV_VAR: "  Europe/Rome "}) == "Europe/Rome")
    check("an unset variable is None", mtz.timezone_id_from_env({}) is None)
    check("an empty variable is None", mtz.timezone_id_from_env({mtz.ENV_VAR: ""}) is None)
    # A typo in an *optional* override must not take the server down: it is
    # read while building a request, so raising would break every write.
    check("an unusable variable reads as unset, not as an error",
          mtz.timezone_id_from_env({mtz.ENV_VAR: '{"tz": "x"}'}) is None)
    check("the variable name is the documented one", mtz.ENV_VAR == "EXCHANGE_TIMEZONE")


# ------------------------------------------------------------------
# Parsing the two configuration response shapes
# ------------------------------------------------------------------


def test_owa_configuration_shape() -> None:
    print("GetOwaUserConfiguration (named keys)")
    payload = {
        "Body": {
            "__type": "GetOwaUserConfigurationResponse:#Exchange",
            "UserOptions": {
                "__type": "UserOptions:#Exchange",
                "TimeZone": "W. Europe Standard Time",
                "WeekStartDay": "Monday",
            },
        }
    }
    check("finds a nested TimeZone key",
          mtz.timezone_id_from_config(payload) == "W. Europe Standard Time",
          repr(mtz.timezone_id_from_config(payload)))

    nested = {"UserOptions": {"TimeZone": {"Id": "Europe/Rome", "Bias": 60}}}
    check("follows a TimeZone whose value is a container",
          mtz.timezone_id_from_config(nested) == "Europe/Rome")

    listed = {"Configurations": [{"Name": "x"}, {"userTimeZone": "Pacific Standard Time"}]}
    check("walks into lists", mtz.timezone_id_from_config(listed) == "Pacific Standard Time")

    # TimeZoneDefinition/timeZoneOffsets are the walk's own furniture: their
    # values are containers, and a substring match on "timezone" would return
    # them. Whole-key matching is what keeps them out.
    furniture = {"TimeZoneDefinition": {"Periods": [{"Bias": "PT0S"}]},
                "timeZoneOffsets": [{"offset": 60}]}
    check("a structural timezone container is not mistaken for an id",
          mtz.timezone_id_from_config(furniture) is None,
          repr(mtz.timezone_id_from_config(furniture)))


def test_ews_dictionary_shape() -> None:
    print("GetUserConfiguration (EWS Dictionary)")
    payload = {
        "Body": {
            "ResponseMessages": {"Items": [{
                "ResponseClass": "Success",
                "UserConfiguration": {
                    "Dictionary": [
                        {"DictionaryKey": {"Type": "String", "Value": ["WeekStartDay"]},
                         "DictionaryValue": {"Type": "String", "Value": ["1"]}},
                        {"DictionaryKey": {"Type": "String", "Value": ["timezone"]},
                         "DictionaryValue": {"Type": "String", "Value": ["W. Europe Standard Time"]}},
                    ]
                },
            }]}
        }
    }
    check("reads the id out of a dictionary entry",
          mtz.timezone_id_from_config(payload) == "W. Europe Standard Time",
          repr(mtz.timezone_id_from_config(payload)))

    # This backend flattens wire shapes elsewhere (GetFolder returns a
    # non-EWS response entirely), so a bare string on either side is accepted.
    flat = {"Dictionary": [{"DictionaryKey": "TimeZone", "DictionaryValue": "Europe/Rome"}]}
    check("accepts a flattened dictionary entry",
          mtz.timezone_id_from_config(flat) == "Europe/Rome")

    unknown_key = {"Dictionary": [{"DictionaryKey": "SomethingElse", "DictionaryValue": "UTC"}]}
    check("a dictionary entry with an unrelated key is not read as a zone",
          mtz.timezone_id_from_config(unknown_key) is None)


def test_unrecognised_payloads_find_nothing() -> None:
    print("Shapes nobody recognises must read as 'found nothing'")
    for payload, why in (
        ({}, "empty object"),
        ({"Body": {"ResponseMessages": {"Items": [{"ResponseClass": "Error"}]}}}, "an error response"),
        ({"Body": {"ErrorCode": 500, "FaultMessage": "NullReferenceException"}}, "an OWA fault"),
        (None, "no payload at all"),
        ("W. Europe Standard Time", "a bare string, not a response"),
        ({"TimeZone": ""}, "a hint key with an empty value"),
        ({"TimeZone": None}, "a hint key with a null value"),
    ):
        check(f"no id from {why}", mtz.timezone_id_from_config(payload) is None,
              repr(mtz.timezone_id_from_config(payload)))

    # A tolerant parser over an unknown shape must not be able to hang.
    deep: dict = {"TimeZone": "Europe/Rome"}
    for _ in range(mtz._MAX_WALK_DEPTH + 5):
        deep = {"Wrapper": deep}
    check("the walk is depth-bounded rather than unbounded",
          mtz.timezone_id_from_config(deep) is None)


# ------------------------------------------------------------------
# Header building
# ------------------------------------------------------------------


def test_header_building() -> None:
    print("The header that goes on the wire")
    with_tz = mtz.request_header("V2017_08_18", "W. Europe Standard Time")
    check("carries the server version", with_tz["RequestServerVersion"] == "V2017_08_18")
    check("carries the zone in TimeZoneContext",
          with_tz["TimeZoneContext"]["TimeZoneDefinition"]["Id"] == "W. Europe Standard Time")
    check("keeps the EWS __type annotations",
          with_tz["__type"] == "JsonRequestHeaders:#Exchange"
          and with_tz["TimeZoneContext"]["__type"] == "TimeZoneContext:#Exchange"
          and with_tz["TimeZoneContext"]["TimeZoneDefinition"]["__type"]
          == "TimeZoneDefinitionType:#Exchange")

    # The task reads depend on the block being *absent*, not on it carrying
    # UTC: without a conversion their UTC-midnight dates round-trip exactly.
    without = mtz.request_header("Exchange2013", None)
    check("omits TimeZoneContext entirely when given no zone",
          "TimeZoneContext" not in without, repr(without))

    check("two calls don't share a mutable header",
          mtz.request_header("Exchange2013", "UTC") is not mtz.request_header("Exchange2013", "UTC"))


def test_describe() -> None:
    print("Describing a resolution to an operator")
    env = mtz.describe(mtz.TimezoneResolution("Europe/Rome", mtz.SOURCE_ENV))
    check("names the env var so the operator knows what to edit", mtz.ENV_VAR in env, env)
    mailbox = mtz.describe(mtz.TimezoneResolution("Europe/Rome", mtz.SOURCE_MAILBOX))
    check("says the mailbox answered", "mailbox" in mailbox.lower(), mailbox)
    fb = mtz.describe(mtz.TimezoneResolution("UTC", mtz.SOURCE_FALLBACK, "GetOwaUserConfiguration: HTTP 500"))
    check("a fallback carries its reason", "HTTP 500" in fb and "fallback" in fb.lower(), fb)
    for resolution in (env, mailbox, fb):
        check(f"the zone itself is in the text: {resolution!r}", "Europe/Rome" in resolution or "UTC" in resolution)


# ------------------------------------------------------------------
# The transport half, against a fake request()
# ------------------------------------------------------------------


class _FakeClient(OWAClient):
    """An OWAClient with request() stubbed out and no BrowserSession at all.

    Subclassed rather than constructed so __init__'s real cache/lock setup is
    what's under test; BrowserSession is bypassed because nothing here needs
    a browser and importing one would need Playwright's browsers installed.
    """

    def __init__(self, responses):
        self.actions: list[str] = []
        self._responses = responses
        self._timezone = None
        import threading
        self._timezone_lock = threading.Lock()

    def request(self, action, payload, *, timeout=30):
        self.actions.append(action)
        outcome = self._responses.get(action, {})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _no_env(monkey: dict) -> None:
    """Make sure a real EXCHANGE_TIMEZONE in the developer's shell can't skew a check."""
    import os
    monkey["saved"] = os.environ.pop(mtz.ENV_VAR, None)


def _restore_env(monkey: dict) -> None:
    import os
    if monkey.get("saved") is not None:
        os.environ[mtz.ENV_VAR] = monkey["saved"]


def test_probe_order_and_caching() -> None:
    print("Resolving against a live-ish OWA")
    monkey: dict = {}
    _no_env(monkey)
    try:
        client = _FakeClient({
            "GetOwaUserConfiguration": {"UserOptions": {"TimeZone": "W. Europe Standard Time"}},
        })
        first = client.mailbox_timezone_detail()
        check("uses the zone OWA reported",
              first.timezone_id == "W. Europe Standard Time" and first.source == mtz.SOURCE_MAILBOX,
              repr(first))
        # The OWA-native action is asked first: the EWS GetUserConfiguration
        # pair is already known to fault on this backend for another config
        # name (see tools/categories.py).
        check("asks the OWA-native action first and stops there",
              client.actions == ["GetOwaUserConfiguration"], repr(client.actions))

        client.mailbox_timezone()
        client.mailbox_timezone()
        check("resolves once per process, not per request",
              client.actions == ["GetOwaUserConfiguration"], repr(client.actions))

        # Falling through to the EWS action when the native one has nothing.
        client2 = _FakeClient({
            "GetOwaUserConfiguration": {"UserOptions": {"WeekStartDay": "Monday"}},
            "GetUserConfiguration": {"Dictionary": [
                {"DictionaryKey": "timezone", "DictionaryValue": "Europe/Rome"}
            ]},
        })
        check("falls through to the EWS action", client2.mailbox_timezone() == "Europe/Rome")
        check("in that order", client2.actions == ["GetOwaUserConfiguration", "GetUserConfiguration"],
              repr(client2.actions))
    finally:
        _restore_env(monkey)


def test_discovery_failures_degrade() -> None:
    print("A failed lookup degrades, it never raises")
    monkey: dict = {}
    _no_env(monkey)
    try:
        # GetUserConfiguration 500s here for another config name already, so a
        # fault is the expected case, not an exotic one. It must not be able to
        # take down every write in the process.
        client = _FakeClient({
            "GetOwaUserConfiguration": RuntimeError("OWA request failed (HTTP 500) (NullReferenceException)"),
            "GetUserConfiguration": RuntimeError("OWA request failed (HTTP 500)"),
        })
        resolution = client.mailbox_timezone_detail()
        check("both actions faulting still yields a usable zone",
              resolution.timezone_id == "UTC" and resolution.source == mtz.SOURCE_FALLBACK,
              repr(resolution))
        check("the detail names both failures so it is diagnosable",
              "GetOwaUserConfiguration" in resolution.detail
              and "GetUserConfiguration" in resolution.detail,
              repr(resolution.detail))
        check("a cached fallback is not re-probed",
              client.mailbox_timezone() == "UTC" and len(client.actions) == 2,
              repr(client.actions))
        check("request_header still builds on the fallback",
              client.request_header("V2017_08_18")["TimeZoneContext"]["TimeZoneDefinition"]["Id"] == "UTC")
    finally:
        _restore_env(monkey)


def test_session_expiry_is_not_cached() -> None:
    print("Asking before sign-in completed is retried, not remembered")
    monkey: dict = {}
    _no_env(monkey)
    try:
        client = _FakeClient({"GetOwaUserConfiguration": SessionExpiredError("Session expired (HTTP 401).")})
        first = client.mailbox_timezone_detail()
        check("an expired session falls back for this call", first.timezone_id == "UTC", repr(first))
        # Caching it would serve UTC for the rest of the process's life over a
        # mailbox that was merely not signed in yet.
        client._responses["GetOwaUserConfiguration"] = {"UserOptions": {"TimeZone": "Europe/Rome"}}
        check("and the next call tries again instead of serving the fallback forever",
              client.mailbox_timezone() == "Europe/Rome")
    finally:
        _restore_env(monkey)


def test_env_override_skips_discovery() -> None:
    print("An override means no lookup at all")
    import os
    saved = os.environ.get(mtz.ENV_VAR)
    os.environ[mtz.ENV_VAR] = "W. Europe Standard Time"
    try:
        client = _FakeClient({"GetOwaUserConfiguration": {"UserOptions": {"TimeZone": "Europe/Rome"}}})
        resolution = client.mailbox_timezone_detail()
        check("the override is used", resolution.timezone_id == "W. Europe Standard Time")
        check("and is reported as the source", resolution.source == mtz.SOURCE_ENV)
        # An operator who set the variable usually did so *because* discovery
        # misbehaves, so spending a request to be overruled is pure cost.
        check("no request is made at all", client.actions == [], repr(client.actions))
    finally:
        if saved is None:
            os.environ.pop(mtz.ENV_VAR, None)
        else:
            os.environ[mtz.ENV_VAR] = saved


def test_request_header_omission() -> None:
    print("with_timezone=False reaches the wire as an omission")
    monkey: dict = {}
    _no_env(monkey)
    try:
        client = _FakeClient({"GetOwaUserConfiguration": {"UserOptions": {"TimeZone": "Europe/Rome"}}})
        read = client.request_header("Exchange2013", with_timezone=False)
        check("no TimeZoneContext block", "TimeZoneContext" not in read, repr(read))
        check("and no lookup was needed to build it", client.actions == [], repr(client.actions))
        write = client.request_header("V2017_08_18")
        check("a write header carries the resolved zone",
              write["TimeZoneContext"]["TimeZoneDefinition"]["Id"] == "Europe/Rome")
    finally:
        _restore_env(monkey)


# ------------------------------------------------------------------
# The regression itself
# ------------------------------------------------------------------


def _legacy_zone_offenders(source: str, label: str = "<source>") -> list[str]:
    """Where `source` uses the legacy zone id as a value rather than as table data.

    Parsed with `ast` rather than matched as text, because the two cases are
    indistinguishable line-by-line and only one of them is a bug:

    - `{"Russian Standard Time": "Europe/Moscow"}` is a **key** in
      `availability_frame`'s Windows->IANA lookup. Moscow is a real timezone and
      that table has to know it; nothing is being sent.
    - `{"Id": "Russian Standard Time"}`, or `tz_id or "Russian Standard Time"`, is
      the bug from issue #8: a value that goes on the wire.

    A text guard flagged the first of those the moment `availability_frame`
    landed -- a check crying wolf about a table it should never have looked at.
    Docstrings and comments are invisible here by construction: the modules are
    *supposed* to explain the bug they used to have.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - a broken module fails elsewhere
        return [f"{label}: unparseable ({exc})"]

    legacy = mtz.LEGACY_HARDCODED_TIMEZONE_ID
    exempt: set[int] = set()
    for node in ast.walk(tree):
        # Every dict key is table data, never a payload value.
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == legacy:
                    exempt.add(id(key))
        # The module's own record of what the literal used to be.
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and "LEGACY_HARDCODED" in target.id:
                    exempt.add(id(node.value))

    return [
        f"{label}:{getattr(node, 'lineno', '?')}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == legacy and id(node) not in exempt
    ]


def test_no_hardcoded_zone_remains() -> None:
    print("The legacy literal from issue #8 is gone from every request builder")
    from pathlib import Path

    # First prove the guard can fire. A check that cannot fail is worthless, and
    # this one silently stopped being able to distinguish the cases once already.
    caught = _legacy_zone_offenders(
        'HEADER = {"TimeZoneContext": {"Id": "Russian Standard Time"}}', "synthetic"
    )
    check("a payload value is caught", len(caught) == 1, repr(caught))
    caught_default = _legacy_zone_offenders(
        'def f(tz_id=None):\n    return tz_id or "Russian Standard Time"\n', "synthetic"
    )
    check("a keyword default is caught", len(caught_default) == 1, repr(caught_default))

    # And that it does not fire on the legitimate shapes.
    table = _legacy_zone_offenders('_MAP = {"Russian Standard Time": "Europe/Moscow"}', "synthetic")
    check("a Windows->IANA table key is not an offender", table == [], repr(table))
    prose = _legacy_zone_offenders('"""Once sent Russian Standard Time."""\n# and in a comment\n')
    check("docstrings and comments are not offenders", prose == [], repr(prose))

    package = Path(mtz.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        offenders += _legacy_zone_offenders(
            path.read_text(encoding="utf-8"), str(path.relative_to(package))
        )
    check("no module puts the legacy zone on the wire", not offenders, "; ".join(offenders))


def main() -> bool:
    for test in (
        test_looks_like_timezone_id,
        test_precedence,
        test_env_reading,
        test_owa_configuration_shape,
        test_ews_dictionary_shape,
        test_unrecognised_payloads_find_nothing,
        test_header_building,
        test_describe,
        test_probe_order_and_caching,
        test_discovery_failures_degrade,
        test_session_expiry_is_not_cached,
        test_env_override_skips_discovery,
        test_request_header_omission,
        test_no_hardcoded_zone_remains,
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
