"""Per-mailbox configuration for the smoke suite, read from the environment.

Every live-mailbox test needs the address of the mailbox it is running against:
they send disposable messages to it, self-invite it to disposable meetings, and
query its own free/busy. That address used to be a literal
`SELF_EMAIL = "<someone>@<company>"` at the top of eight test modules -- which
put one real person's address into a **public** repository eight times over, and
made every one of those modules unusable against any other mailbox without
editing it first.

So it lives in `$EXCHANGE_SMOKE_SELF_EMAIL` instead, resolved here:

    EXCHANGE_SMOKE_SELF_EMAIL=you@example.com python -m tests.smoke.tests.test_email_lifecycle

`require_self_email()` is the normal entry point and is deliberately strict --
unset means the suite stops with a recorded failure row naming the variable,
rather than guessing. Guessing is the one thing that must not happen here: these
suites *send mail* and *create calendar items*, so a wrong address is not a
failed test, it's a message delivered to a stranger.

`discover_self_email()` adds one fallback on top for the suite that already had
it (`test_email_flag.py`): read the address back off a message in Sent Items,
whose `from` is by definition this mailbox. It costs a mailbox round-trip and
needs a non-empty Sent Items, which is why it isn't the default.

Note what is deliberately *not* here: no default value, and no reading of
`.env.local`. `.env.local` is loaded by `exchange_mcp/server.py` inside the
*server* process; these helpers run in the *test* process, which may well be
talking to a server someone else started (see server_manager.py). Making this
file read a config the server also reads would let the two disagree silently.
"""

import os

from tests.smoke.results import record

SELF_EMAIL_ENV = "EXCHANGE_SMOKE_SELF_EMAIL"

# The remediation is factored out and both notes kept short on purpose:
# `record()` truncates a note at 300 characters, and the part that must survive
# is the tail -- the variable to set and where the rule is written down.
_SET_IT = f"set {SELF_EMAIL_ENV}=you@example.com and re-run (see tests/smoke/config.py)"
_MISSING_NOTE = f"this mailbox's own address is not configured -- {_SET_IT}"
_UNDISCOVERABLE_NOTE = (
    f"could not determine this mailbox's own address from Sent Items -- {_SET_IT}"
)


def self_email() -> str | None:
    """The mailbox's own SMTP address from the environment, or None if unset."""
    return os.environ.get(SELF_EMAIL_ENV, "").strip() or None


def require_self_email(label: str) -> str | None:
    """Same as `self_email()`, but records a failure row when it is unset.

    Returns None on failure; callers are expected to `return False` from their
    `main()` immediately, exactly as they would for any other failed step. The
    row goes through `record()` so the miss shows up in `.state/results.jsonl`
    and on stdout like every other outcome -- an import-time raise would produce
    neither.

    `label` is the step that needed the address (usually the tool about to be
    called), so the recorded row reads in context.
    """
    address = self_email()
    if not address:
        record(label, {"env": SELF_EMAIL_ENV}, "TOOL_ERROR", _MISSING_NOTE)
    return address


async def discover_self_email(s, label: str) -> str | None:
    """`require_self_email()` plus a read-back fallback via Sent Items.

    Prefers `$EXCHANGE_SMOKE_SELF_EMAIL`; failing that, reads the address off a
    recent message in Sent Items, whose `from` is by definition this mailbox.
    Records a failure row and returns None only when both fail.

    Imported lazily (see the import inside) because `mcp_client` pulls in
    `server_manager`, and a test module that only needs the strict variant
    shouldn't drag the server machinery in through `config`.
    """
    configured = self_email()
    if configured:
        return configured

    from tests.smoke.mcp_client import call

    listing = await call(s, "get_emails", folder="Sent", limit=3, include_body=True)
    if isinstance(listing, dict):
        for row in listing.get("emails", []):
            address = (row.get("from") or "").strip()
            if "@" in address:
                return address

    record(label, {"env": SELF_EMAIL_ENV}, "TOOL_ERROR", _UNDISCOVERABLE_NOTE)
    return None
