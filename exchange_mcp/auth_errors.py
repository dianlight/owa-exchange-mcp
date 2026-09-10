"""Diagnosis of OWA / Entra ID (AAD) / ADFS sign-in problems.

The server never types a password: authentication happens either silently (the
persistent browser profile is still signed in) or interactively, in a visible
browser window the user drives themselves. So this module does *not* decide
whether to keep retrying a credential — there is no credential. It exists to
answer one question well: when an interactive sign-in didn't complete in time,
*why* not, and what should the user do about it.

That means classification is advisory and runs exactly once, at the end of a
timed-out login (`BrowserSession._async_interactive_login`), never mid-flow.
Bailing out early would be wrong here: a human sitting in front of the window
who mistypes a password, denies an MFA push by accident, or gets redirected to
a change-password page can simply carry on in that same window — so we keep
waiting for the session to appear rather than second-guessing them.

Split into its own module so it stays pure and unit-testable
(tests/unit/test_auth_errors.py) with no Playwright import.
"""

import re

# ------------------------------------------------------------------
# Reason codes
# ------------------------------------------------------------------

# No usable session and nobody has signed in yet. This is what tools report
# when they can't reach the mailbox; the `login` tool is the way out.
INTERACTIVE_LOGIN_REQUIRED = "interactive_login_required"

# A login window was opened but the sign-in wasn't finished in time, with no
# recognizable error on the page (user stepped away, slow 2FA, etc.).
LOGIN_TIMEOUT = "login_timeout"

# Recognized conditions on the sign-in page, used to explain a LOGIN_TIMEOUT.
INVALID_CREDENTIALS = "invalid_credentials"
ACCOUNT_NOT_FOUND = "account_not_found"
PASSWORD_EXPIRED = "password_expired"
ACCOUNT_LOCKED = "account_locked"
ACCOUNT_DISABLED = "account_disabled"
MFA_DENIED = "mfa_denied"
MFA_SETUP_REQUIRED = "mfa_setup_required"
BLOCKED_BY_POLICY = "blocked_by_policy"

# Something below the auth layer broke (network, browser crash).
TRANSIENT = "transient"

# What the user should actually do. Surfaced verbatim by the `login` tool,
# `check_session`, and the startup log, so a client hitting this gets the
# remediation without reading the source.
REMEDIATION: dict[str, str] = {
    INTERACTIVE_LOGIN_REQUIRED: "Call the `login` tool: it opens a browser window on the OWA "
                                "sign-in page. Complete the sign-in there (including 2FA), then "
                                "call `login` again to confirm.",
    LOGIN_TIMEOUT: "Call the `login` tool again to reopen the sign-in window and finish signing in.",
    INVALID_CREDENTIALS: "The sign-in page rejected the password that was entered. Call `login` "
                         "again and re-enter it in the browser window.",
    ACCOUNT_NOT_FOUND: "The sign-in page didn't recognize that account. Call `login` again and "
                       "check the address you type in the browser window.",
    PASSWORD_EXPIRED: "The account password has expired or must be changed. Complete the "
                      "change-password flow in the login window (or in a normal browser), then "
                      "call `login` again.",
    ACCOUNT_LOCKED: "The account is locked (too many failed sign-ins). Wait for the lockout to "
                    "clear or have it unlocked, then call `login` again.",
    ACCOUNT_DISABLED: "The account is disabled or blocked. Contact your Exchange administrator — "
                      "signing in again won't help.",
    MFA_DENIED: "The multi-factor authentication prompt was denied or went unanswered. Call "
                "`login` again and approve it.",
    MFA_SETUP_REQUIRED: "The identity provider needs additional MFA registration. Complete it in "
                        "the login window, then call `login` again.",
    BLOCKED_BY_POLICY: "A conditional-access / device-compliance policy blocked this sign-in. This "
                       "mailbox may not be reachable from an automated browser profile.",
    TRANSIENT: "A network or browser error interrupted the sign-in. Call `login` again to retry.",
}

# ------------------------------------------------------------------
# Detection hints
# ------------------------------------------------------------------

# Entra ID / AAD error codes. Matched against the whole login-page HTML
# (they live in the page's `$Config.serverError`, not always in visible text)
# because a bare `AADSTS<digits>` token is precise enough that a stray match
# isn't a realistic worry — unlike free English text, see _PAGE_TEXT_HINTS.
_AADSTS_REASONS: dict[str, str] = {
    "AADSTS50126": INVALID_CREDENTIALS,   # invalid username or password
    "AADSTS50056": INVALID_CREDENTIALS,   # no/invalid password stored for the user
    "AADSTS50034": ACCOUNT_NOT_FOUND,     # user account does not exist in the directory
    "AADSTS50055": PASSWORD_EXPIRED,      # password is expired
    "AADSTS50144": PASSWORD_EXPIRED,      # AD password expired, must be changed
    "AADSTS50053": ACCOUNT_LOCKED,        # smart lockout / too many failed attempts
    "AADSTS50057": ACCOUNT_DISABLED,      # user account is disabled
    "AADSTS50058": MFA_SETUP_REQUIRED,    # silent sign-in failed, interactive auth needed
    "AADSTS50072": MFA_SETUP_REQUIRED,    # user must enroll in MFA
    "AADSTS50074": MFA_DENIED,            # strong authentication required, not satisfied
    "AADSTS50079": MFA_SETUP_REQUIRED,    # user must enroll for MFA (proof-up)
    "AADSTS500121": MFA_DENIED,           # authentication failed during strong auth request
    "AADSTS53000": BLOCKED_BY_POLICY,     # device not compliant
    "AADSTS53001": BLOCKED_BY_POLICY,     # device not domain-joined
    "AADSTS53003": BLOCKED_BY_POLICY,     # blocked by conditional access
    "AADSTS700016": BLOCKED_BY_POLICY,    # application not found in directory
}

# Free-text hints, matched *only* against the login page's visible error
# containers (see VISIBLE_ERROR_SELECTORS) rather than the full HTML: phrases
# like "update your password" also occur in hidden AAD templates that are
# present on a perfectly healthy sign-in page.
_PAGE_TEXT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (PASSWORD_EXPIRED, (
        "your password has expired",
        "password has expired",
        "you must change your password",
        "update your password",
        "change your password",
    )),
    (ACCOUNT_LOCKED, (
        "your account has been temporarily locked",
        "account is temporarily locked",
        "you've tried to sign in too many times",
        "too many failed sign-in attempts",
    )),
    (ACCOUNT_DISABLED, (
        "your account has been disabled",
        "account is disabled",
        "your account is blocked",
    )),
    (ACCOUNT_NOT_FOUND, (
        "we couldn't find an account",
        "this username may be incorrect",
        "that account doesn't exist",
        "enter a valid email address",
    )),
    (INVALID_CREDENTIALS, (
        "your account or password is incorrect",
        "password is incorrect",
        "incorrect user id or password",
        "invalid username or password",
        "the user name or password you entered isn't correct",
        "isn't correct. if you don't remember",
    )),
    (MFA_DENIED, (
        "the request was denied",
        "sign-in was denied",
        "you denied the request",
        "we didn't hear from you",
        "verification failed",
    )),
    (MFA_SETUP_REQUIRED, (
        "more information is required",
        "your organization needs more information",
        "action required",
    )),
    (BLOCKED_BY_POLICY, (
        "you can't get there from here",
        "blocked by your organization",
        "device is not compliant",
    )),
)

# URL substrings that mean the identity provider parked us on an interactive
# change-password flow. Only meaningful as a timeout *explanation*: an expired
# password on classic OWA redirects to a change-password page on the OWA host
# itself, so "we reached the OWA host" alone doesn't mean we're signed in.
_URL_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (PASSWORD_EXPIRED, (
        "changepassword",
        "/updatepassword",
        "passwordreset",
        "expiredpassword",
        "/owa/auth/expiredpassword",
    )),
)

# Login-page containers that hold a *rendered* error message. Union of the
# Entra ID / AAD sign-in page, on-prem ADFS, and classic OWA forms-based auth.
VISIBLE_ERROR_SELECTORS: tuple[str, ...] = (
    "#passwordError",
    "#usernameError",
    "#idTD_Error",
    "#loginErrorMessage",
    "#errorText",
    "#error",
    "#errorMessage",
    "div.signInError",
    ".alert-error",
    "[role='alert']",
)


def classify_login_failure(page_text: str = "", page_url: str = "", page_html: str = "") -> tuple[str, str] | None:
    """Map a sign-in page's error surface to a (reason, message) pair.

    Args:
        page_text: Concatenated text of the page's *visible* error containers
            (see VISIBLE_ERROR_SELECTORS). Free-text hints are matched against
            this only — never the whole document.
        page_url: The page's current URL, for change-password parking pages.
        page_html: Full page HTML, scanned for `AADSTS<code>` tokens only.

    Returns:
        (reason, human-readable message), or None when nothing recognizable is
        on the page — the caller then reports a plain LOGIN_TIMEOUT.
    """
    lowered_text = (page_text or "").lower()
    lowered_url = (page_url or "").lower()

    for raw_code in re.findall(r"AADSTS\d+", page_html or "", flags=re.IGNORECASE):
        code = raw_code.upper()
        reason = _AADSTS_REASONS.get(code)
        if not reason:
            continue
        detail = (page_text or "").strip()
        if not detail:
            return reason, code
        return reason, detail if code in detail.upper() else f"{detail} [{code}]"

    for reason, hints in _URL_HINTS:
        if any(hint in lowered_url for hint in hints):
            return reason, f"The sign-in page is parked on a password-change step ({page_url})."

    for reason, hints in _PAGE_TEXT_HINTS:
        if any(hint in lowered_text for hint in hints):
            return reason, page_text.strip()

    return None


def remediation(reason: str | None) -> str:
    return REMEDIATION.get(reason or "", REMEDIATION[INTERACTIVE_LOGIN_REQUIRED])


class AuthenticationRequiredError(Exception):
    """Raised when OWA can't be reached until someone signs in interactively.

    Deliberately *not* a subclass of SessionExpiredError: that one means "the
    cookie went stale, re-check the profile and retry", which OWAClient handles
    transparently. This one means "the profile has no usable session and there
    is no password to replay" — the caller must surface it so the user (or the
    agent, via the `login` tool) can open a sign-in window.
    """

    def __init__(self, message: str, reason: str = INTERACTIVE_LOGIN_REQUIRED):
        self.reason = reason
        self.remediation = remediation(reason)
        super().__init__(f"{message} {self.remediation}".strip())
