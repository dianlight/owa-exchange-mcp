"""Unit test: sign-in failure diagnosis and profile-directory resolution.

Unlike tests/smoke/, this needs no live mailbox, no browser and no
EXCHANGE_OWA_URL -- everything under test is pure logic.

The classifier's job is to explain a *timed-out* interactive sign-in, so the
false-negative cases (recognizing a real error) and the false-positive cases
(never inventing one from a healthy page) both matter: a wrong verdict here is
the difference between "your account is locked" and a useless "sign-in didn't
complete".

Run standalone:
    python -m tests.unit.test_auth_errors
"""

import sys
from pathlib import Path

from exchange_mcp import auth_errors as ae
from exchange_mcp import browser_session as bs

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


def test_aadsts_codes() -> None:
    print("AADSTS error codes (read from the page's $Config, not visible text)")
    cases = {
        "AADSTS50126": ae.INVALID_CREDENTIALS,
        "AADSTS50034": ae.ACCOUNT_NOT_FOUND,
        "AADSTS50055": ae.PASSWORD_EXPIRED,
        "AADSTS50053": ae.ACCOUNT_LOCKED,
        "AADSTS50057": ae.ACCOUNT_DISABLED,
        "AADSTS500121": ae.MFA_DENIED,
        "AADSTS53003": ae.BLOCKED_BY_POLICY,
    }
    for code, expected in cases.items():
        html = '<script>$Config={"strServiceExceptionMessage":"%s: something went wrong"};</script>' % code
        result = ae.classify_login_failure(page_html=html)
        check(f"{code} -> {expected}", result is not None and result[0] == expected, repr(result))

    # An unmapped code must not be guessed at - a plain LOGIN_TIMEOUT is more
    # honest than a made-up reason.
    unknown = ae.classify_login_failure(page_html="AADSTS99999999: brand new failure mode")
    check("unmapped AADSTS code -> None", unknown is None, repr(unknown))


def test_visible_text_hints() -> None:
    print("Visible error text")
    cases = [
        ("Your account or password is incorrect. If you don't remember...", ae.INVALID_CREDENTIALS),
        ("Your password has expired and must be changed.", ae.PASSWORD_EXPIRED),
        ("Your account has been temporarily locked to prevent unauthorized use.", ae.ACCOUNT_LOCKED),
        ("We couldn't find an account with that username.", ae.ACCOUNT_NOT_FOUND),
        ("Incorrect user ID or password. Type the correct user ID and password.", ae.INVALID_CREDENTIALS),
        ("The request was denied.", ae.MFA_DENIED),
        ("You can't get there from here.", ae.BLOCKED_BY_POLICY),
    ]
    for text, expected in cases:
        result = ae.classify_login_failure(page_text=text)
        check(f"{expected} <- {text[:40]!r}", result is not None and result[0] == expected, repr(result))


def test_url_hints() -> None:
    print("Change-password parking pages")
    # Classic OWA parks this on the OWA host itself, so "we reached the OWA
    # host" is not by itself evidence of a completed sign-in.
    for url in (
        "https://owa.example.com/owa/auth/expiredpassword.aspx?url=%2fowa",
        "https://login.microsoftonline.com/common/ChangePassword",
        "https://owa.example.com/owa/auth/passwordreset.aspx",
    ):
        result = ae.classify_login_failure(page_url=url)
        check(f"password_expired <- {url[:50]}", result is not None and result[0] == ae.PASSWORD_EXPIRED, repr(result))


def test_no_false_positives() -> None:
    print("Healthy pages must not be classified as failures")
    check("empty page -> None", ae.classify_login_failure() is None)

    healthy_url = "https://login.microsoftonline.com/common/oauth2/authorize?client_id=x"
    check("plain sign-in URL -> None", ae.classify_login_failure(page_url=healthy_url) is None)

    # The AAD sign-in page ships hidden templates for flows it isn't currently
    # showing. Free-text hints are matched against the visible error containers
    # only, so this HTML must stay unclassified.
    hidden_template = (
        '<div id="idDiv_PWD_Update" style="display:none">Update your password</div>'
        '<div id="idDiv_Locked" hidden>Your account has been temporarily locked</div>'
    )
    result = ae.classify_login_failure(page_text="", page_html=hidden_template)
    check("hidden AAD templates in HTML -> None", result is None, repr(result))

    mfa_prompt = "We've sent a notification to your mobile device. Please open the Microsoft Authenticator app to respond."
    check("MFA prompt text -> None", ae.classify_login_failure(page_text=mfa_prompt) is None)


def test_remediation() -> None:
    print("Remediation text")
    check("every reason code has remediation",
          all(r in ae.REMEDIATION for r in (
              ae.INTERACTIVE_LOGIN_REQUIRED, ae.LOGIN_TIMEOUT, ae.INVALID_CREDENTIALS,
              ae.ACCOUNT_NOT_FOUND, ae.PASSWORD_EXPIRED, ae.ACCOUNT_LOCKED,
              ae.ACCOUNT_DISABLED, ae.MFA_DENIED, ae.MFA_SETUP_REQUIRED,
              ae.BLOCKED_BY_POLICY, ae.TRANSIENT,
          )))
    check("unknown reason falls back to the login-tool instruction",
          "`login`" in ae.remediation("something-new"))
    check("no remediation mentions a removed CLI",
          all("login.py" not in text for text in ae.REMEDIATION.values()),
          str([r for r, t in ae.REMEDIATION.items() if "login.py" in t]))


def test_error_carries_remediation() -> None:
    print("AuthenticationRequiredError")
    exc = ae.AuthenticationRequiredError("Session expired (HTTP 401).")
    check("defaults to interactive_login_required", exc.reason == ae.INTERACTIVE_LOGIN_REQUIRED)
    check("message points at the login tool", "`login`" in str(exc), str(exc))

    typed = ae.AuthenticationRequiredError("nope", ae.ACCOUNT_LOCKED)
    check("explicit reason preserved", typed.reason == ae.ACCOUNT_LOCKED)


def test_default_profile_dir() -> None:
    print("Default profile directory")
    resolved = bs.default_profile_dir()
    package_parent = Path(bs.__file__).resolve().parent.parent
    in_checkout = (package_parent / "pyproject.toml").exists()

    check("absolute path", resolved.is_absolute(), str(resolved))
    check("ends in .browser-profile", resolved.name == ".browser-profile", str(resolved))
    if in_checkout:
        # Running from the repo: the profile must stay with the working tree, so a
        # developer's signed-in session isn't stranded by this test suite's own
        # import path.
        check("source checkout keeps the repo profile", resolved == package_parent / ".browser-profile",
              str(resolved))
    else:
        check("installed package uses ~/owa-mcp", resolved == Path.home() / "owa-mcp" / ".browser-profile",
              str(resolved))

    # The installed-package branch, exercised regardless of how this test itself
    # is being run, by pointing the detection at a directory with no pyproject.
    original = bs.__file__
    try:
        bs.__file__ = str(Path.home() / "site-packages-stand-in" / "exchange_mcp" / "browser_session.py")
        check("no pyproject next to package -> ~/owa-mcp",
              bs.default_profile_dir() == Path.home() / "owa-mcp" / ".browser-profile",
              str(bs.default_profile_dir()))
    finally:
        bs.__file__ = original


def main() -> bool:
    for test in (
        test_aadsts_codes,
        test_visible_text_hints,
        test_url_hints,
        test_no_false_positives,
        test_remediation,
        test_error_carries_remediation,
        test_default_profile_dir,
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
