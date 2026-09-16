"""Unit test: the startup session probe and the interactive-login poll.

Needs no live mailbox, no browser and no EXCHANGE_OWA_URL. `BrowserSession` is
subclassed with `__init__` skipped, so no Chromium is launched and no background
loop thread is started; the coroutines under test are driven directly with
`asyncio.run` against stubbed internals.

Covers the two halves of the startup false negative in PROJECT_STATUS.md §4,
plus the budget arithmetic that produced the useless `TimeoutError: .`:

- **The probe answered "not signed in" on a session that was there.** One
  attempt is not decisive on a cold profile - the SPA may not have minted a
  token yet - and the asymmetry says which way to lean: a false "no" opens a
  sign-in window nobody asked for and replaces a working server with a
  minutes-long wait, while a false "yes" costs one failed request that
  `_relogin_or_raise()` already retries and reports. So most checks below push
  on "a negative must be retried before it is believed".
- **The poll reloaded the page the user was typing into.** The expensive
  confirmation calls `page.reload()`, so running it every tick is hostile.
  Several checks assert it is *not* called - "did not happen" is the whole
  property here.
- **A wrapper budget smaller than the work it wraps** is what threw away the
  login diagnosis. Those are constants, so they are asserted as arithmetic:
  this regressed twice, once while the fix itself was being written.

Run standalone:
    python -m tests.unit.test_login_probe
"""

import asyncio
import sys

from exchange_mcp import auth_errors as ae
from exchange_mcp import browser_session as bs

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


class _StubPage:
    def __init__(self, url: str = "https://outlook.office365.com/mail/"):
        self.url = url


class _Session(bs.BrowserSession):
    """A BrowserSession with no browser, no thread, and scriptable internals.

    `__init__` is bypassed deliberately: the real one launches a daemon event-loop
    thread per instance, and none of the logic under test needs it.
    """

    def __init__(self, *, canary=False, bearer_valid=False, url=None, checks=None):
        self.owa_url = "https://owa.example.com"
        self.headless = False
        self._anchor_page = _StubPage(url) if url is not None else _StubPage()
        self._login_lock = None
        self._auth_mode = "bearer" if bearer_valid else "canary"
        self._bearer = {"authorization": "Bearer x"} if bearer_valid else {}
        self._bearer_expiry = (asyncio.get_event_loop_policy() and 0) or 0
        if bearer_valid:
            import time
            self._bearer_expiry = time.time() + 3600
        self._canary = canary
        # Scripted results for successive _async_warm_and_check() calls; a bare
        # exception instance in the list is raised instead of returned.
        self._scripted = list(checks or [])
        self.warm_and_check_calls = 0
        self.warm_anchor_calls = 0
        self.expensive_calls = 0

    # --- stubbed internals -------------------------------------------------

    async def _async_is_session_valid(self) -> bool:
        return self._canary

    async def _async_warm_anchor(self) -> None:
        self.warm_anchor_calls += 1

    async def _async_has_active_session(self) -> bool:
        self.expensive_calls += 1
        return bool(self._canary)

    async def _async_warm_and_check(self) -> bool:
        self.warm_and_check_calls += 1
        if not self._scripted:
            return False
        outcome = self._scripted.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return bool(outcome)

    async def _async_detect_login_failure(self, page):
        return None

    async def _async_relaunch(self, headless: bool) -> None:  # pragma: no cover
        raise AssertionError("no relaunch expected in these tests")


# ------------------------------------------------------------------
# Budget arithmetic - the part that regressed twice
# ------------------------------------------------------------------


def test_budgets_cover_the_work_they_wrap() -> None:
    print("Every budget is larger than the work it wraps")
    post_deadline = bs._SESSION_PROBE_ATTEMPT_SECONDS + bs._LOGIN_DIAGNOSIS_SECONDS
    check("interactive_login's margin covers the final check plus the diagnosis",
          bs._LOGIN_RUN_MARGIN_SECONDS >= post_deadline,
          f"margin {bs._LOGIN_RUN_MARGIN_SECONDS} vs work {post_deadline}")
    check("...with actual slack, not exactly equal",
          bs._LOGIN_RUN_MARGIN_SECONDS > post_deadline,
          "a zero-slack margin is what lost the reason in the first place")

    # The original bug: `+ 30` equalled the diagnosis's own worst case
    # (10 selectors x 3 nodes x two 500ms calls), so there was nothing left.
    worst_case_diagnosis = len(ae.VISIBLE_ERROR_SELECTORS) * 3 * 1.0
    check("the old hardcoded 30s margin really was the diagnosis's worst case",
          abs(worst_case_diagnosis - 30.0) < 0.01,
          f"{worst_case_diagnosis}s - the arithmetic the comment cites")
    check("and the diagnosis is now capped below that worst case",
          bs._LOGIN_DIAGNOSIS_SECONDS < worst_case_diagnosis,
          f"{bs._LOGIN_DIAGNOSIS_SECONDS} vs {worst_case_diagnosis}")

    check("the probe budget is the sum of its bounded parts",
          bs._SESSION_PROBE_BUDGET_SECONDS == (
              bs._SESSION_PROBE_ATTEMPTS * bs._SESSION_PROBE_ATTEMPT_SECONDS
              + (bs._SESSION_PROBE_ATTEMPTS - 1) * bs._SESSION_PROBE_SETTLE_SECONDS
          ),
          str(bs._SESSION_PROBE_BUDGET_SECONDS))
    check("has_active_session's derived default exceeds that budget",
          bs._SESSION_PROBE_RUN_MARGIN_SECONDS > 0
          and (bs._SESSION_PROBE_BUDGET_SECONDS + bs._SESSION_PROBE_RUN_MARGIN_SECONDS)
          > bs._SESSION_PROBE_BUDGET_SECONDS)
    check("the confirmation floor outlasts a bearer capture, so two can't overlap",
          bs._LOGIN_CONFIRM_MIN_INTERVAL_SECONDS >= 20.0,
          str(bs._LOGIN_CONFIRM_MIN_INTERVAL_SECONDS))
    check("more than one attempt is made at all",
          bs._SESSION_PROBE_ATTEMPTS >= 2, str(bs._SESSION_PROBE_ATTEMPTS))


# ------------------------------------------------------------------
# looks_like_signin_url
# ------------------------------------------------------------------


def test_looks_like_signin_url() -> None:
    print("Telling 'still signing in' from 'moved on'")
    for url in (
        "https://login.microsoftonline.com/common/oauth2/authorize?x=1",
        "https://login.live.com/oauth20",
        "https://sts.windows.net/tenant/",
        "https://mytenant.b2clogin.com/x",
        "https://adfs.corp.example.com/adfs/ls/?wa=wsignin1.0",
        "https://api-a.duosecurity.com/frame/prompt",
        # Classic OWA signs in on the mailbox host itself, so a host-only check
        # would call this "moved on" and reload the form under the user.
        "https://owa.example.com/owa/auth/logon.aspx?replaceCurrent=1",
        "https://owa.example.com/owa/auth.owa",
    ):
        check(f"still signing in: {url[:52]}", ae.looks_like_signin_url(url))

    for url in (
        "https://outlook.office365.com/mail/",
        "https://outlook.cloud.microsoft/mail/inbox",
        "https://owa.example.com/owa/",
    ):
        check(f"moved on: {url}", not ae.looks_like_signin_url(url))

    # An unidentifiable page must read as "keep waiting quietly", never as a cue
    # to go and reload something we can't even name.
    for url, why in (("", "empty"), ("   ", "blank"), ("about:blank", "about:blank"), (None, "None")):
        check(f"unknown page keeps waiting: {why}", ae.looks_like_signin_url(url))


# ------------------------------------------------------------------
# The probe retries a negative
# ------------------------------------------------------------------


def test_probe_retries_a_negative() -> None:
    print("A 'not signed in' verdict is only believed after retrying")
    s = _Session(checks=[True])
    check("a positive first attempt returns immediately",
          asyncio.run(s._async_probe_active_session()) is True)
    check("and costs exactly one attempt", s.warm_and_check_calls == 1, str(s.warm_and_check_calls))

    # The observed bug: the session is there, the first look just misses it.
    s = _Session(checks=[False, True])
    check("a session found on the second look is reported as present",
          asyncio.run(s._async_probe_active_session()) is True)
    check("which took two attempts", s.warm_and_check_calls == 2, str(s.warm_and_check_calls))

    s = _Session(checks=[False, False])
    check("only an all-negative run answers 'not signed in'",
          asyncio.run(s._async_probe_active_session()) is False)
    check("having used the full attempt allowance",
          s.warm_and_check_calls == bs._SESSION_PROBE_ATTEMPTS, str(s.warm_and_check_calls))

    # A raising attempt is indistinguishable from a negative one here, and both
    # want another look - it must not short-circuit into a verdict.
    s = _Session(checks=[RuntimeError("mid-navigation"), True])
    check("an exception on the first attempt does not become a verdict",
          asyncio.run(s._async_probe_active_session()) is True)
    check("the retry still happened", s.warm_and_check_calls == 2, str(s.warm_and_check_calls))


def test_probe_attempt_is_bounded() -> None:
    print("A hung attempt is abandoned, not fatal")
    saved = bs._SESSION_PROBE_ATTEMPT_SECONDS
    bs._SESSION_PROBE_ATTEMPT_SECONDS = 0.05
    try:
        async def hang() -> bool:
            await asyncio.sleep(5)
            return True

        s = _Session(checks=[hang, True])
        # Without the per-attempt ceiling this would block for 5s and the
        # *wrapper* budget would be the thing that fired - which is the failure
        # shape being eliminated: bound the work, then derive the wrapper.
        check("the hung attempt is cut short and the next one decides",
              asyncio.run(s._async_probe_active_session()) is True)
        check("both attempts ran", s.warm_and_check_calls == 2, str(s.warm_and_check_calls))
    finally:
        bs._SESSION_PROBE_ATTEMPT_SECONDS = saved


# ------------------------------------------------------------------
# The quiet poll signal never touches the page
# ------------------------------------------------------------------


def test_poll_signal_is_non_destructive() -> None:
    print("The poll's quiet check navigates nothing")
    s = _Session(canary=True)
    check("a canary cookie is conclusive", asyncio.run(s._async_poll_session_signal()) is True)
    check("and needed no page touch at all",
          s.warm_anchor_calls == 0 and s.expensive_calls == 0,
          f"warm={s.warm_anchor_calls} expensive={s.expensive_calls}")

    s = _Session(bearer_valid=True, url="https://login.microsoftonline.com/x")
    check("a live bearer token we already hold counts, even on a sign-in URL",
          asyncio.run(s._async_poll_session_signal()) is True)
    check("still no page touch", s.expensive_calls == 0)

    s = _Session(url="https://login.microsoftonline.com/common/login")
    check("parked on the sign-in flow with nothing held: keep waiting",
          asyncio.run(s._async_poll_session_signal()) is False)
    check("and crucially, no reload of the form the user is typing into",
          s.expensive_calls == 0 and s.warm_anchor_calls == 0)

    s = _Session(url="https://outlook.office365.com/mail/")
    check("a page that has left the sign-in flow triggers a real check",
          asyncio.run(s._async_poll_session_signal()) is True)
    check("but the trigger itself is not the confirmation", s.expensive_calls == 0)

    class _Exploding(_Session):
        @property
        def _anchor_page(self):
            raise RuntimeError("navigating")

        @_anchor_page.setter
        def _anchor_page(self, value):
            pass

    s = _Exploding()
    check("an unreadable page keeps waiting instead of raising",
          asyncio.run(s._async_poll_session_signal()) is False)


# ------------------------------------------------------------------
# The login loop
# ------------------------------------------------------------------


def test_login_loop_does_not_reload_every_tick() -> None:
    print("The login poll leaves a signing-in user alone")

    class _Quiet(_Session):
        """Quiet signal always false: the user is still on the sign-in page."""

        async def _async_poll_session_signal(self) -> bool:
            return False

    s = _Quiet(checks=[False])  # pre-lock check says "no session yet"
    result = asyncio.run(s._async_interactive_login(timeout=0.3, poll_seconds=0.05))
    check("it times out rather than claiming success", result["success"] is False)
    check("with a proper reason, not an exception",
          result.get("reason") == ae.LOGIN_TIMEOUT, repr(result.get("reason")))
    # The whole point: ~6 ticks happened and not one of them reloaded the page.
    # Before this, every tick called the reloading check.
    check("the reloading confirmation was never called during the poll",
          s.expensive_calls == 0, str(s.expensive_calls))


def test_login_loop_rate_limits_the_confirmation() -> None:
    print("Even a triggering page is confirmed at most once per floor")

    class _AlwaysTrigger(_Session):
        async def _async_poll_session_signal(self) -> bool:
            return True

    # Floor left at its real value, far longer than the test's own window, so
    # only the first tick may confirm no matter how many ticks elapse.
    s = _AlwaysTrigger(checks=[False, False, False])
    asyncio.run(s._async_interactive_login(timeout=0.3, poll_seconds=0.05))
    check("many ticks produced exactly one reloading confirmation",
          s.expensive_calls == 1, str(s.expensive_calls))


def test_login_loop_final_check_catches_a_late_session() -> None:
    print("A session that landed late is not reported as a failure")

    class _QuietButSignedIn(_Session):
        async def _async_poll_session_signal(self) -> bool:
            return False  # the loop never notices

    # Pre-lock check says no; the final authoritative check says yes. That is the
    # §4 symptom exactly - "the poll said no, the next request said yes".
    s = _QuietButSignedIn(checks=[False, True])
    result = asyncio.run(s._async_interactive_login(timeout=0.2, poll_seconds=0.05))
    check("the late session is reported as success", result["success"] is True,
          repr(result))
    check("and the message says when it was confirmed",
          "expired" in result.get("message", "").lower(), repr(result.get("message")))


def test_login_loop_reports_a_diagnosed_failure() -> None:
    print("A recognised sign-in error still reaches the caller")

    class _Failing(_Session):
        async def _async_poll_session_signal(self) -> bool:
            return False

        async def _async_detect_login_failure(self, page):
            return ae.PASSWORD_EXPIRED, "Your password has expired."

    s = _Failing(checks=[False, False])
    result = asyncio.run(s._async_interactive_login(timeout=0.15, poll_seconds=0.05))
    check("the diagnosed reason wins over the plain timeout",
          result.get("reason") == ae.PASSWORD_EXPIRED, repr(result.get("reason")))
    check("and carries its message", "expired" in result.get("error", "").lower(),
          repr(result.get("error")))


def test_login_diagnosis_cannot_outlive_its_budget() -> None:
    print("A slow diagnosis degrades to the plain timeout")
    saved = bs._LOGIN_DIAGNOSIS_SECONDS
    bs._LOGIN_DIAGNOSIS_SECONDS = 0.05
    try:
        class _SlowDiagnosis(_Session):
            async def _async_poll_session_signal(self) -> bool:
                return False

            async def _async_detect_login_failure(self, page):
                await asyncio.sleep(5)
                return ae.PASSWORD_EXPIRED, "never gets here"

        s = _SlowDiagnosis(checks=[False, False])
        result = asyncio.run(s._async_interactive_login(timeout=0.15, poll_seconds=0.05))
        # It must still *answer*. Previously an over-running diagnosis let the
        # outer budget fire, and the bare TimeoutError discarded everything.
        check("an over-running diagnosis still yields a timeout result",
              result["success"] is False and result.get("reason") == ae.LOGIN_TIMEOUT,
              repr(result))
        check("the answer is never replaced by an exception", "error" in result)
    finally:
        bs._LOGIN_DIAGNOSIS_SECONDS = saved


def main() -> bool:
    for test in (
        test_budgets_cover_the_work_they_wrap,
        test_looks_like_signin_url,
        test_probe_retries_a_negative,
        test_probe_attempt_is_bounded,
        test_poll_signal_is_non_destructive,
        test_login_loop_does_not_reload_every_tick,
        test_login_loop_rate_limits_the_confirmation,
        test_login_loop_final_check_catches_a_late_session,
        test_login_loop_reports_a_diagnosed_failure,
        test_login_diagnosis_cannot_outlive_its_budget,
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
