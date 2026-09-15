"""Persistent Chromium browser session for the Exchange MCP server.

OWA now appears to require signals only a real browser produces (a fresh
per-page CSRF canary, Sec-Fetch/Referer headers, real TLS/JS fingerprint)
that a plain requests.Session replaying exported cookies can't replicate.
This module keeps one Chromium instance alive for the life of the process,
backed by a persistent on-disk profile so cookies/SSO state (and Microsoft's
"stay signed in" cookie) survive restarts. Every OWA call opens its own tab,
performs its fetch through that tab's real page context, and closes it.

Playwright's async API is required for this pattern (page.expect_response
alongside page.evaluate), but the rest of the codebase calls into OWAClient
synchronously. To bridge that without touching any tool code, this module
runs a dedicated background thread with its own asyncio loop hosting
Playwright, and exposes plain synchronous methods that hop onto that loop
via asyncio.run_coroutine_threadsafe(...).result().
"""

import asyncio
import base64
import json as _json
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from exchange_mcp.auth_errors import (  # noqa: F401  (re-exported for callers)
    INTERACTIVE_LOGIN_REQUIRED,
    LOGIN_TIMEOUT,
    TRANSIENT,
    VISIBLE_ERROR_SELECTORS,
    AuthenticationRequiredError,
    classify_login_failure,
)
from exchange_mcp.profile_lock import (  # noqa: F401  (re-exported for callers)
    PROFILE_LOCKED,
    ProfileLockedError,
    ProfileLockState,
    classify_launch_failure,
    clear_stale_lock,
    inspect_profile_lock,
)

# Where the persistent Chromium profile lives when EXCHANGE_BROWSER_PROFILE_DIR
# isn't set. An installed package must not write into site-packages, so it uses
# a stable per-user location; a source checkout keeps its profile in the repo so
# a developer's signed-in session and their working tree stay together.
_INSTALLED_PROFILE_DIR = Path.home() / "owa-mcp" / ".browser-profile"


def is_source_checkout() -> bool:
    """True when this package is being run from a source tree, not site-packages.

    Detected by pyproject.toml sitting next to the package directory. Note that
    an *editable* install (`pip install -e .`) is still a source checkout by this
    test, because its `exchange_mcp` package resolves back into the repo - which
    is why `pip install -e .` keeps using `<repo>/.browser-profile` rather than
    the per-user path.
    """
    return (Path(__file__).resolve().parent.parent / "pyproject.toml").exists()


def default_profile_dir() -> Path:
    """Resolve the default profile directory for this deployment.

    A source checkout keeps its profile in the repo, so a developer's signed-in
    session travels with their working tree. Anything else (a real installed
    package) uses the per-user path: site-packages is the wrong place, and often
    unwritable, for a browser profile.
    """
    if is_source_checkout():
        return Path(__file__).resolve().parent.parent / ".browser-profile"
    return _INSTALLED_PROFILE_DIR


# How long a launch waits for the profile directory's lock to free before giving
# up and reporting it as owned by someone else. Two budgets, because the two
# callers know different things:
#
# - A relaunch (headless<->visible, or crash recovery) has *just* closed this
#   process's own browser, and Chromium releases the profile lock as that process
#   exits, trailing context.close(). Waiting out our own exit is normal, so the
#   budget is generous.
# - A cold launch has no such expectation: a lock that is held now is almost
#   certainly another server's, and every extra second is added to every tool
#   call that retries. Just enough to ride out a previous run that is still
#   shutting down.
#
# Both budgets end in the same place - still held, so refuse to launch - and that
# uniformity is deliberate even though a relaunch could instead shrug and share
# the profile the way it used to. A machine slow enough to hold its own lock past
# the relaunch budget would then get two browsers on one user-data-dir, silently;
# the alternative is a self-describing PROFILE LOCKED line. Visible-and-wrong
# beats invisible-and-wrong, and this budget is the knob if that ever fires
# spuriously.
_LOCK_WAIT_RELAUNCH_SECONDS = 15.0
_LOCK_WAIT_LAUNCH_SECONDS = 3.0
_LOCK_POLL_SECONDS = 0.5

_CRASH_HINTS = (
    "target closed",
    "browser has been closed",
    "connection closed",
    "has been closed",
    # The context/anchor page is briefly None while _async_relaunch swaps
    # headless mode, so a tool call landing in that window sees an
    # AttributeError on None rather than a Playwright error. That used to only
    # happen after a genuine crash; since the interactive login relaunches
    # visible as a matter of course, it's now a routine (if narrow) race, and
    # _run_with_recovery's relaunch-and-retry-once is exactly the right handling.
    "'nonetype' object has no attribute",
)

# Copilot's chat UI is served from a *different origin* than OWA and rendered
# in an iframe - confirmed by discovery capture 20260911-112708-e917, where
# every recorded Copilot click reported one of these hosts as its frame URL
# while the surrounding app was on outlook.office365.com. Matched as substrings
# so a tenant/cloud variant of the host still resolves.
_COPILOT_FRAME_HOST_HINTS = (
    "m365copilotapp.svc.cloud.microsoft",
    "m365copilotapp",
    "copilot.cloud.microsoft",
)

# Copilot has no documented API - text hints scraped from its own chat pane
# are the only signal available for these conditions. Update if the live
# wording turns out different (see the Copilot module's discovery-spike notes).
_COPILOT_RATE_LIMIT_HINTS = (
    "unable to respond",
    "try again later",
    "too many requests",
    "high demand",
)
_COPILOT_SIGNIN_HINTS = (
    "sign in",
    "session has expired",
    "you've been signed out",
)
# The pane renders in the mailbox's display language (the capture ran on an
# it-IT tenant), so an English-only "Stop generating" regex silently never
# matches and the generation-complete check degrades to text-stability alone.
# Add localisations here rather than in the polling code.
_COPILOT_STOP_HINTS = (
    "stop",         # en
    "interrompi",   # it
    "arrêter",      # fr
    "detener",      # es
    "beenden",      # de
    "parar",        # pt
)

# Interactive roles whose aria-label is an affordance name ("Send", "Interrompi
# la generazione") rather than content. Only these get their label recorded by
# _async_copilot_pane_structure - a label on a message bubble can *be* the
# generated answer, and a structural diagnostic must not carry mailbox content.
_COPILOT_LABEL_SAFE_ROLES = frozenset({
    "button", "textbox", "tab", "menuitem", "link", "combobox", "searchbox",
})


# Role markers Copilot's chat transcript prints before each turn. Observed live
# on 2026-09-11 (it-IT tenant) as literal English "You said:" / "Copilot said:"
# next to a *localised* disclaimer, so these appear not to be translated - but
# that is one tenant's evidence, not a guarantee, hence a table rather than a
# literal. Everything after the last answer marker is the newest reply.
_COPILOT_ANSWER_MARKERS = (
    "copilot said:",
    "copilot ha detto:",   # it
    "copilot a dit :",     # fr
    "copilot dijo:",       # es
    "copilot sagte:",      # de
)

# Trailing "AI-generated content may be incorrect"-style disclaimer. This one *is*
# localised (observed: "Il contenuto generato dall'IA potrebbe...").
_COPILOT_DISCLAIMER_HINTS = (
    "ai-generated content",
    "ai generated content",
    "contenuto generato dall",   # it
    "contenu généré par l",      # fr
    "contenido generado por",    # es
    "ki-generierte inhalte",     # de
)

# Lines that are pure transcript furniture rather than message text: the bare
# product name printed next to the avatar, the "You said:" marker, and date
# separators. Matched exactly (case-insensitively) so a message that merely
# mentions Copilot is untouched.
_COPILOT_CHROME_LINES = frozenset({
    "copilot", "you said:", "you said", "oggi", "today", "aujourd'hui",
    "hoy", "heute", "hoje",
})

# "Generation is in flight" status lines Copilot prints while working, before any
# answer turn exists. Observed live 2026-09-15 on an it-IT tenant as "In corso…"
# sitting above an app-promo block ("Scarica l'app per dispositivi mobili
# Copilot"), a state that is both *new* relative to the baseline and *stable* for
# seconds - so it satisfied the old settle rule and was returned as the answer.
#
# Matched as a whole line (normalised for case and trailing ellipsis), never as a
# substring, and that restriction is load-bearing: the very summary this bug hid
# contained "In corso" as an action-item *status* inside a table row. inner_text()
# renders those cells tab-separated, so the status never forms its own line -
# a substring test would have rejected the correct answer as unfinished.
_COPILOT_PROGRESS_HINTS = frozenset({
    "in corso",         # it
    "working on it",
    "thinking",
    "generating",
    "searching",
    "sto cercando",     # it
    "recherche",        # fr
    "buscando",         # es
    "suche",            # de
})


# Trailing punctuation a status line may carry: the ellipsis Copilot actually
# uses ("In corso…"), its ASCII spelling, and a colon.
_COPILOT_PROGRESS_TRAILERS = "….: \t"

# The authoritative "still generating" signal, and the only one here that is not
# a localisation table. Established by a 444-sample DOM probe of a live pane on
# 2026-09-15 (PROJECT_STATUS.md §4), which is worth stating precisely because two
# text-based versions of the same false-success bug had already shipped:
#
#   aria-busy="true"        present samples 0-87, t=0.0-48.0s   <- exact window
#   stop control visible    present samples 0-87, t=0.0-48.0s   <- tracks it
#   data-testid=loading-message   visible 444/444               <- useless
#   final answer text       first seen t=49.4s
#
# So aria-busy covered the generating window exactly: zero false positives and
# zero false negatives over the whole run, where the *text* went flat at 377
# chars for 27 consecutive polls (~14s) while generation was very much in flight
# and the real answer (8836 chars) was still 46 seconds away. `loading-message`
# reads like the obvious candidate and is a trap - it is a permanent element, not
# a state flag.
#
# The ~1.4s lag between the flag clearing and the last text landing is why text
# stability is still required *after* it clears, rather than replaced by it.
_COPILOT_BUSY_SELECTOR = '[aria-busy="true"]'

# How long the settle loop waits between polls, and how many consecutive
# unchanged polls it needs. Named rather than inlined because the two counts are
# meaningless without the interval - and because a test driving the real loop
# would otherwise have to sleep for real seconds to exercise it.
_COPILOT_POLL_SECONDS = 1.0
# Two numbers because the two settle paths rest on different evidence: with
# aria-busy, or a new "Copilot said:" turn, we know a real answer exists; without
# either we are baseline-diffing, the weaker signal that produced the 2026-09-15
# false success, so it has to hold still for longer.
_COPILOT_SETTLE_POLLS = 2
_COPILOT_SETTLE_POLLS_NO_MARKER = 4

# Copilot replaces its own iframe during load, and no amount of checking the
# frame *before* typing keeps it alive while we type: two smoke runs on
# 2026-09-15 each lost one call in five to "Frame was detached" raised from
# inside the composer, with the failing tool rotating between them. So the
# acquire-and-submit step retries as a unit instead - the pane locator and the
# baseline read both belong to the dead frame and have to be redone together.
_COPILOT_SUBMIT_ATTEMPTS = 3
_COPILOT_FRAME_SWAP_PAUSE = 1.5
# Worst-case seconds acquire-and-submit can burn before generation even starts,
# added to copilot_ask's own budget so a retried submit can't eat the caller's
# generation timeout.
_COPILOT_ACQUIRE_BUDGET = 75

# Playwright's wording for "the thing you were holding no longer exists". All of
# these mean the same thing here - re-resolve the pane and try again - and none
# of them means the prompt was rejected.
_COPILOT_FRAME_SWAP_HINTS = (
    "frame was detached",
    "frame got detached",
    "execution context was destroyed",
    "target closed",
    "target page, context or browser has been closed",
    "no textbox to type into",       # our own message for a half-built replacement
    "never offered a textbox",       # ditto, from _async_copilot_open_pane
)


def _copilot_is_frame_swap(exc: Exception) -> bool:
    """Is this exception the iframe being replaced under us, rather than a real fault?"""
    message = str(exc).lower()
    return any(hint in message for hint in _COPILOT_FRAME_SWAP_HINTS)


def _copilot_answer_marker_index(lines: list[str]) -> int | None:
    """Index of the *last* "Copilot said:"-style role marker, or None.

    Shared by the answer extractor and the settle check so they can never
    disagree about whether a turn exists - which matters, because "there is no
    answer marker yet" is precisely how the settle check knows Copilot has not
    answered.
    """
    marker_at = None
    for i, line in enumerate(lines):
        lowered = line.strip().lower()
        if any(lowered.startswith(m) for m in _COPILOT_ANSWER_MARKERS):
            marker_at = i
    return marker_at


def _copilot_answer_marker_count(text: str) -> int:
    """How many "Copilot said:"-style turns the transcript shows.

    A *count*, not a presence test, because the pane is reused across calls and
    keeps its history: after the first tool call there is always a marker from a
    previous turn. Presence therefore answers "has Copilot ever answered in this
    conversation", when the question is "has it answered *this* prompt" - and the
    settle rule that asked the former took its permissive path on every call
    after the first, disabling the progress-line guard exactly when a long
    multi-turn session makes it most necessary.
    """
    count = 0
    for line in text.splitlines():
        lowered = line.strip().lower()
        if any(lowered.startswith(m) for m in _COPILOT_ANSWER_MARKERS):
            count += 1
    return count


def _copilot_has_progress_line(text: str) -> bool:
    """True when any line of `text` is, on its own, an in-flight status marker."""
    for line in text.splitlines():
        normalised = line.strip().lower().rstrip(_COPILOT_PROGRESS_TRAILERS)
        if normalised and normalised in _COPILOT_PROGRESS_HINTS:
            return True
    return False


def _copilot_answer_text(
    baseline: str, text: str, prompt: str = "", chrome_lines: set[str] | None = None
) -> str:
    """Isolate Copilot's answer from the pane's full text.

    `pane.inner_text()` is the whole iframe: date separator, role markers, the
    echoed prompt, the answer, and a footer disclaimer. Returning all of that as
    a tool's `text` reports Copilot's own UI as its answer - the first live run
    produced exactly that:

        "Oggi\\nYou said:\\nCopilot said:\\nCopilot\\nPONG\\nIl contenuto generato..."

    when the answer was the single word "PONG".

    Two strategies, in order. The transcript's own role marker is preferred:
    everything after the *last* "Copilot said:" is the newest reply, which is
    exactly what a caller wants and survives a multi-turn pane. If no marker is
    recognised (unlisted localisation, or a redesigned pane), fall back to
    diffing against `baseline` - the pane's text from before submitting - which
    needs no knowledge of the transcript's wording.

    Either way the result is stripped of transcript furniture and the trailing
    disclaimer, and falls back to the full text rather than "" if that leaves
    nothing: an empty string would read as a successful empty answer, whereas
    the full text is at least inspectable.

    `chrome_lines` is for furniture that can only be identified structurally -
    in practice the pane's button labels, gathered by the caller. Copilot renders
    follow-up suggestion chips *after* the answer ("Start a new question",
    "Summarize a topic", "Draft a message" - observed live 2026-09-11), so they
    are neither in `baseline` nor before the answer marker, and their wording is
    generated per answer rather than fixed. They are buttons, though, which is a
    property no hint table can go stale on.
    """
    raw_lines = [line.strip() for line in text.splitlines()]
    prompt_lines = {line.strip() for line in prompt.splitlines() if line.strip()}
    extra_chrome = {line.strip() for line in (chrome_lines or set()) if line.strip()}

    marker_at = _copilot_answer_marker_index(raw_lines)

    # Lines already on screen before submitting are furniture by definition.
    # This is what removes the composer's own placeholder ("Invia un messaggio a
    # Copilot" on an it-IT tenant), which sits *below* the transcript and so
    # survives the marker cut - and it removes it without a table of localised
    # placeholder strings, because the placeholder was in `baseline` already.
    seen = {line.strip() for line in baseline.splitlines() if line.strip()}

    if marker_at is not None:
        candidate = raw_lines[marker_at + 1:]
    else:
        candidate = raw_lines

    answer = [
        line for line in candidate
        if line
        and line not in seen
        and line not in extra_chrome
        and line.lower() not in _COPILOT_CHROME_LINES
        and line not in prompt_lines
        and not any(hint in line.lower() for hint in _COPILOT_DISCLAIMER_HINTS)
    ]

    return "\n".join(answer).strip() or text.strip()


class SessionExpiredError(Exception):
    """Raised when the OWA session has expired (HTTP 401/440 or HTML redirect)."""


class BearerModeRequiredError(Exception):
    """Raised by post_substrate() when the session is in canary (classic OWA) auth mode.

    Callers should catch this and fall back to the equivalent EWS action
    instead - see OWAClient.find_people() / people.py's find_person().
    """


class CopilotUnavailableError(Exception):
    """Raised when Copilot itself reports a capacity/rate-limit condition.

    Distinct from SessionExpiredError (auth problem) and a plain RuntimeError
    (broken selector/unexpected DOM) - see copilot_ask().
    """


class BrowserResponse:
    """Minimal requests.Response-like wrapper around a captured Playwright response."""

    def __init__(self, status_code: int, headers: dict, body: bytes):
        self.status_code = status_code
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return _json.loads(self.content)


class BrowserSession:
    """Owns one persistent Chromium context, reused across all OWA calls."""

    def __init__(self, owa_url: str, headless: bool = True, profile_dir: str | Path | None = None):
        self.owa_url = owa_url.rstrip("/")
        self.owa_host = urlparse(self.owa_url).netloc

        # `headless` is the *preference*, not necessarily the current state: an
        # interactive login has to be visible, so it relaunches the context with
        # headless=False (see interactive_login). self.headless always reflects
        # how the live context was actually launched.
        self.headless_preference = headless
        self.headless = headless

        self.profile_dir = Path(profile_dir).expanduser() if profile_dir else default_profile_dir()
        # Recorded before anything creates it, so startup can honestly report
        # "reusing an existing profile" vs. "created a new one".
        self.profile_existed = self.profile_dir.exists()

        # Last lock verdict for this profile, and any stale singleton artifacts
        # cleared on the way in. Diagnostics for the operator, not control flow:
        # every launch re-inspects, because ownership changes the moment the other
        # server exits and a verdict cached from startup would be worse than none.
        # `profile_lock_cleared` is sticky on purpose -- it answers "did this
        # process ever have to clean up after a dead browser", which a later
        # uneventful relaunch shouldn't erase.
        self.profile_lock_state: ProfileLockState | None = None
        self.profile_lock_cleared: list[str] = []

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="owa-browser")
        self._thread.start()

        self._playwright = None
        self._context = None
        self._anchor_page = None
        self._context_closed = True  # forces a launch on first use
        self._launch_lock: asyncio.Lock | None = None  # created lazily, on the browser loop
        self._login_lock: asyncio.Lock | None = None  # created lazily, on the browser loop

        # "Modern Outlook" (e.g. outlook.cloud.microsoft) auth: some tenants
        # have migrated to a backend that ignores the classic X-OWA-CANARY
        # cookie entirely and instead requires an OAuth Bearer JWT, which the
        # already-authenticated SPA mints for itself via MSAL. We don't run
        # our own OAuth flow - we just capture a live token from the page's
        # own traffic and reuse it. auth_mode starts "canary" (works for
        # every classic OWA deployment) and flips to "bearer" the first time
        # a request against this session comes back with no canary cookie.
        self._auth_mode = "canary"
        self._bearer: dict = {}
        self._bearer_expiry = 0.0
        self._bearer_lock: asyncio.Lock | None = None
        self._request_counter = 0

        self._copilot_lock: asyncio.Lock | None = None  # created lazily, on the browser loop

    @property
    def auth_mode(self) -> str:
        """"canary" (classic cookie CSRF token) or "bearer" (OAuth JWT, modern Outlook)."""
        return self._auth_mode

    @property
    def bearer_origin(self) -> str:
        """Origin of the modern Outlook SPA once bearer auth has been captured, else owa_url."""
        return self._bearer.get("origin") or self.owa_url

    # ------------------------------------------------------------------
    # Loop plumbing
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _run_with_recovery(self, coro_factory, timeout: float):
        """Run a coroutine; if the browser/context died, relaunch and retry once.

        ProfileLockedError is re-raised untouched, ahead of the crash check, and
        that ordering is the point of issue #11: a contended profile and a crashed
        browser produce the *same* "has been closed" text, so the generic recovery
        happily relaunched into the same lock and failed identically -- turning one
        legible problem into two indistinguishable ones. A live lock cannot be
        retried out of, so the error propagates with its own remediation instead.
        """
        try:
            return self._run(coro_factory(), timeout=timeout)
        except ProfileLockedError:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if any(hint in msg for hint in _CRASH_HINTS):
                self._context_closed = True
                # Relaunch budget, not the launch one: our own browser has just
                # died and its process tree may still be releasing the profile.
                self._run(
                    self._async_ensure_context(lock_wait=_LOCK_WAIT_RELAUNCH_SECONDS),
                    timeout=120,
                )
                return self._run(coro_factory(), timeout=timeout)
            raise

    def start(self, timeout: float = 120) -> None:
        """Launch the persistent browser context. Call once at server startup."""
        self._run(self._async_ensure_context(), timeout=timeout)

    def stop(self, timeout: float = 30) -> None:
        """Close the browser and stop the background loop."""
        try:
            self._run(self._async_shutdown(), timeout=timeout)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)

    # ------------------------------------------------------------------
    # Context lifecycle
    # ------------------------------------------------------------------

    async def _async_await_profile_lock(self, budget: float) -> ProfileLockState:
        """Wait for the profile directory to stop being owned by another browser.

        Polls rather than checking once, because the common held-lock case here is
        entirely legitimate and transient: this process's own Chromium releasing
        the profile as it exits, a beat behind context.close(). Waiting is also
        what tells the two cases apart without any ownership bookkeeping -- our own
        exit clears within a second or two, another running server never does.

        A STALE verdict (a named owner that is gone) is cleared here; HELD and
        UNKNOWN are returned as found. Only HELD blocks the caller, so a probe
        that reached no verdict costs nothing.
        """
        state = inspect_profile_lock(self.profile_dir)
        deadline = time.monotonic() + max(budget, 0.0)
        while state.blocks_launch and time.monotonic() < deadline:
            await asyncio.sleep(_LOCK_POLL_SECONDS)
            state = inspect_profile_lock(self.profile_dir)

        removed = clear_stale_lock(state)
        if removed:
            self.profile_lock_cleared = removed
            state = inspect_profile_lock(self.profile_dir)

        self.profile_lock_state = state
        return state

    async def _async_ensure_context(self, lock_wait: float = _LOCK_WAIT_LAUNCH_SECONDS) -> None:
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()

        async with self._launch_lock:
            if not self._context_closed and self._context is not None:
                return

            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            # Before mkdir, so a profile that doesn't exist yet reads as free
            # rather than as a directory with no recognizable lock files in it.
            lock_state = await self._async_await_profile_lock(lock_wait)
            if lock_state.blocks_launch:
                # Fail fast and say why. Launching anyway is the tempting
                # alternative and it is what happens today by accident -- verified
                # 2026-09-15, Playwright's Chromium does *not* refuse a second
                # launch on a contended profile, it just quietly shares it. Two
                # browsers writing one user-data-dir is what Chromium's own
                # ProcessSingleton exists to prevent.
                raise ProfileLockedError(lock_state)

            self.profile_dir.mkdir(parents=True, exist_ok=True)

            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    headless=self.headless,
                    viewport={"width": 1280, "height": 900},
                )
            except Exception as exc:
                # Second chance at the lock diagnosis, for the case the pre-check
                # can't see: a browser that lost the race *during* launch, or a
                # platform where the directory probe reached no verdict but
                # Chromium itself said "profile in use".
                after = inspect_profile_lock(self.profile_dir)
                verdict = classify_launch_failure(str(exc), after)
                if verdict:
                    raise ProfileLockedError(after, message=verdict[1]) from exc
                raise

            self._context_closed = False
            self._context.on("close", self._on_context_closed)

            pages = self._context.pages
            self._anchor_page = pages[0] if pages else await self._context.new_page()
            try:
                await self._anchor_page.goto(f"{self.owa_url}/owa/", wait_until="commit", timeout=30000)
            except Exception:
                pass  # _async_warm_anchor() re-navigates as needed

    def _on_context_closed(self) -> None:
        self._context_closed = True

    async def _async_shutdown_context(self) -> None:
        """Close just the browser context, keeping the Playwright driver alive.

        Used by _async_relaunch to switch headless mode on the same profile -
        closing the context flushes cookies to disk, so the session survives.
        """
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        self._context = None
        self._anchor_page = None
        self._context_closed = True

    async def _async_shutdown(self) -> None:
        await self._async_shutdown_context()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def _async_is_session_valid(self) -> bool:
        cookies = await self._context.cookies()
        return any(c["name"] == "X-OWA-CANARY" for c in cookies)

    async def _async_has_active_session(self) -> bool:
        """True if the anchor page is already authenticated, in either auth mode.

        Classic OWA sets the X-OWA-CANARY cookie once signed in. The modern
        Outlook backend (see auth_mode) never sets that cookie at all, so a
        cookie-only check always reports "no session" there even when the
        SPA is genuinely still signed in via SSO - confirm that case by
        checking whether the SPA can still mint a live Bearer token on its
        own, which fails (times out) if a login page is actually showing.
        """
        await self._async_ensure_context()
        if await self._async_is_session_valid():
            return True
        return await self._async_capture_bearer_context()

    async def _async_warm_anchor(self) -> None:
        """Navigate the anchor page to /owa/ so a session check can be trusted.

        Shared by every session check and by the interactive login, because the
        touch itself is load-bearing on the modern Outlook backend - see
        _async_ensure_logged_in's docstring for why.
        """
        await self._async_ensure_context()
        try:
            await self._anchor_page.goto(f"{self.owa_url}/owa/", wait_until="networkidle", timeout=30000)
        except Exception:
            pass  # the caller retries navigation as needed

    async def _async_probe_active_session(self) -> bool:
        await self._async_warm_anchor()
        return await self._async_has_active_session()

    async def _async_ensure_logged_in(self) -> dict:
        """Silent re-auth only: confirm (or silently re-acquire) a session.

        There is no password to submit anywhere in this server, so this can only
        ever succeed on signals the profile already carries - live OWA cookies,
        Microsoft's "stay signed in" cookie, or an SSO session the SPA can still
        mint a Bearer token from. When it fails, the only way forward is an
        interactive sign-in (interactive_login), which a human has to drive.

        The warm-up navigation matters and isn't skippable: on the modern Outlook
        backend the *first* top-level navigation to /owa/ on a freshly-launched
        page lands on an interactive login/account-selection redirect even with a
        perfectly valid SSO session - only a *second* touch of the page (the
        reload() inside _async_capture_bearer_context) lets the SPA silently
        reacquire a token.
        """
        await self._async_warm_anchor()
        if await self._async_has_active_session():
            return {"success": True, "message": "Session active."}
        return {
            "success": False,
            "error": "The browser profile has no usable OWA session.",
            "reason": INTERACTIVE_LOGIN_REQUIRED,
        }

    async def _async_detect_login_failure(self, page) -> tuple[str, str] | None:
        """Read the sign-in page's error surface and classify it (see auth_errors).

        Runs only after an interactive login has already timed out, purely to
        explain why. Text is collected from the page's rendered error containers
        rather than the whole document: the AAD sign-in page ships hidden
        templates whose wording ("update your password", ...) is present even on
        a perfectly healthy page.
        """
        fragments: list[str] = []
        for selector in VISIBLE_ERROR_SELECTORS:
            try:
                locator = page.locator(selector)
                for i in range(min(await locator.count(), 3)):
                    node = locator.nth(i)
                    if not await node.is_visible(timeout=500):
                        continue
                    text = (await node.inner_text(timeout=500) or "").strip()
                    if text and text not in fragments:
                        fragments.append(text)
            except Exception:
                continue

        try:
            html = await page.content()
        except Exception:
            html = ""
        try:
            url = page.url
        except Exception:
            url = ""

        return classify_login_failure(page_text=" ".join(fragments), page_url=url, page_html=html)

    async def _async_relaunch(self, headless: bool) -> None:
        """Close the context and launch it again on the same profile, changing headless mode.

        The profile directory is what carries the session, so closing and
        relaunching keeps whatever we were signed into - context.close() flushes
        cookies to disk on the way out.
        """
        await self._async_shutdown_context()
        # Chromium releases the profile's singleton lock as its process exits,
        # which trails context.close() slightly. Relaunching into that gap fails
        # with a lock error that looks like an unrelated crash (PROJECT_STATUS.md
        # §4), so give it a moment.
        #
        # The sleep stays even though _async_ensure_context now waits on the lock
        # *condition* rather than guessing: the wait can only help where the probe
        # reaches a verdict (it reports UNKNOWN on a profile with no recognizable
        # lock files), and this fixed floor is the mitigation that has been working.
        # Belt and braces, cheap either way -- _LOCK_WAIT_RELAUNCH_SECONDS is what
        # actually covers a slow exit.
        await asyncio.sleep(1)
        self.headless = headless
        await self._async_ensure_context(lock_wait=_LOCK_WAIT_RELAUNCH_SECONDS)

    async def _async_interactive_login(self, timeout: float, poll_seconds: float) -> dict:
        """Open a visible sign-in window and wait for the user to complete it.

        This is the only login path left: the server has no credentials to type,
        so a human (or their SSO/2FA devices) does the actual signing in. We just
        make the window visible, park it on the OWA sign-in page, and poll until a
        session materializes.

        Deliberately does *not* bail out early on a recognized error. Someone is
        sitting in front of this window: a mistyped password, an accidentally
        denied MFA push, or a redirect to a change-password page are all things
        they can just carry on from. Aborting on the first error text would cut
        them off mid-sign-in. Classification happens once, on timeout, only to
        explain what the page was showing when we gave up.
        """
        if self._login_lock is None:
            self._login_lock = asyncio.Lock()

        async with self._login_lock:
            # Another caller may have finished a login while we waited on the lock.
            if await self._async_probe_active_session():
                return {"success": True, "message": "Session active.", "browser_shown": False}

            if self.headless:
                await self._async_relaunch(headless=False)

            page = self._anchor_page
            try:
                await page.bring_to_front()
            except Exception:
                pass
            await self._async_warm_anchor()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(poll_seconds)
                try:
                    if await self._async_has_active_session():
                        # Left visible on purpose: relaunching headless here would
                        # mean tearing down the context seconds after the session
                        # landed in it, and a window on screen is a much cheaper
                        # failure mode than a login that doesn't stick. Restart the
                        # server to get back to headless.
                        return {
                            "success": True,
                            "message": "Signed in successfully. The browser window stays visible "
                                       "for the rest of this server's lifetime; restart the server "
                                       "to return to headless.",
                            "browser_shown": True,
                        }
                except Exception:
                    continue  # mid-navigation; try again on the next tick

            failure = await self._async_detect_login_failure(page)
            if failure:
                reason, message = failure
                return {"success": False, "error": message, "reason": reason, "browser_shown": True}
            return {
                "success": False,
                "error": f"Sign-in was not completed within {int(timeout)} seconds.",
                "reason": LOGIN_TIMEOUT,
                "browser_shown": True,
            }

    def ensure_logged_in(self, timeout: float = 120) -> dict:
        """Confirm or silently re-acquire a session. Never opens a window, never raises.

        Returns {"success": True, ...} or {"success": False, "error", "reason"}.
        Callers that need a session and can't get one this way should surface
        AuthenticationRequiredError (see OWAClient._relogin_or_raise) rather than
        popping up a browser window inside an unrelated tool call.
        """
        return self._run_with_recovery(self._async_ensure_logged_in, timeout=timeout)

    def interactive_login(self, timeout: float = 300, poll_seconds: float = 2) -> dict:
        """Show a browser window and wait up to `timeout` seconds for a sign-in.

        Blocking: call it from a worker thread (asyncio.to_thread) or a background
        task, never inline in an MCP request handler - `timeout` is minutes, not
        milliseconds. The extra 30s on the internal deadline lets the coroutine's
        own timeout report a proper reason instead of dying on _run()'s wait.
        """
        return self._run_with_recovery(
            lambda: self._async_interactive_login(timeout, poll_seconds), timeout=timeout + 30
        )

    def has_active_session(self, timeout: float = 120) -> bool:
        """True if the persistent profile is still signed in, in either auth mode.

        The startup check: "can we reuse this profile as-is, or does someone have
        to sign in?"
        """
        return self._run_with_recovery(self._async_probe_active_session, timeout=timeout)

    # ------------------------------------------------------------------
    # JSON requests
    # ------------------------------------------------------------------

    async def _async_current_canary(self) -> str:
        cookies = await self._context.cookies()
        for c in cookies:
            if c["name"] == "X-OWA-CANARY":
                return c["value"]
        return ""

    @staticmethod
    def _decode_jwt_exp(token: str) -> float | None:
        """Best-effort decode of a JWT's `exp` claim, without signature verification."""
        try:
            payload_b64 = token.split(".")[1]
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(padded))
            return float(claims["exp"])
        except Exception:
            return None

    async def _async_capture_bearer_context(self, timeout: float = 20.0) -> bool:
        """Capture a live Authorization header + session headers from the SPA's own traffic.

        We don't perform OAuth ourselves: the already-authenticated page mints
        a Bearer token via MSAL on its own, silently, using existing SSO
        cookies. Reloading the anchor page makes it refire its bootstrap
        service.svc calls, which we intercept and read the headers off of.
        """
        page = self._anchor_page
        loop = asyncio.get_event_loop()
        found: asyncio.Future = loop.create_future()

        def on_request(request) -> None:
            # "/owa/published/service.svc" is a lower-privilege bootstrap
            # endpoint (used for pre-auth config calls like
            # GetTimeZoneOffsets) - it returns a Bearer token too, but reusing
            # that token/path for mailbox actions like FindConversation gets
            # a 401 AuthError. Only capture from the real mail-action path.
            is_mail_action_url = request.url.endswith("/owa/service.svc") or "/owa/service.svc?" in request.url
            if found.done() or not is_mail_action_url:
                return
            headers = request.headers
            auth = headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return
            parsed = urlparse(request.url)
            base_path = parsed.path.split("service.svc")[0] + "service.svc"
            found.set_result({
                "authorization": auth,
                "x-anchormailbox": headers.get("x-anchormailbox", ""),
                "x-tenantid": headers.get("x-tenantid", ""),
                "x-owa-sessionid": headers.get("x-owa-sessionid", ""),
                "origin": f"{parsed.scheme}://{parsed.netloc}",
                "base_path": base_path,
            })

        page.on("request", on_request)
        try:
            try:
                await page.reload(wait_until="networkidle", timeout=timeout * 1000)
            except Exception:
                pass
            try:
                captured = await asyncio.wait_for(found, timeout=timeout)
            except asyncio.TimeoutError:
                return False

            # The modern Outlook SPA keeps redirecting/navigating internally
            # for a few seconds after "networkidle" fires (e.g. /owa/ ->
            # /mail/). Firing our own fetch() from this page immediately
            # after reload() races against that and invalidates the
            # response body. Settle once more before returning control.
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(3)
        finally:
            page.remove_listener("request", on_request)

        self._bearer = captured
        exp = self._decode_jwt_exp(captured["authorization"].split(" ", 1)[1])
        self._bearer_expiry = exp if exp else (time.time() + 3000)
        self._auth_mode = "bearer"
        return True

    async def _async_ensure_auth(self) -> None:
        """Make sure we have a usable auth mechanism before firing a request.

        Classic OWA deployments use a cookie canary and never hit the bearer
        path at all. Tenants migrated to the "new Outlook" backend drop the
        canary cookie entirely, which we detect here and switch to capturing
        (and, on expiry, refreshing) an OAuth Bearer token instead.
        """
        if self._bearer_lock is None:
            self._bearer_lock = asyncio.Lock()

        async with self._bearer_lock:
            if self._auth_mode == "bearer":
                if time.time() < self._bearer_expiry - 120:
                    return
                if await self._async_capture_bearer_context():
                    return
                self._auth_mode = "canary"  # refresh failed; fall back and re-detect

            if await self._async_current_canary():
                return

            await self._async_capture_bearer_context()

    def _bearer_request_headers(self, action: str) -> dict:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Action": action,
            "Authorization": self._bearer.get("authorization", ""),
            "x-anchormailbox": self._bearer.get("x-anchormailbox", ""),
            "x-tenantid": self._bearer.get("x-tenantid", ""),
            "x-owa-sessionid": self._bearer.get("x-owa-sessionid", ""),
            "x-req-source": "Mail",
            "x-owa-actionsource": action,
            "prefer": 'IdType="ImmutableId", exchange.behavior="IncludeThirdPartyOnlineMeetingProviders"',
            "x-owa-hosted-ux": "false",
            "X-Requested-With": "XMLHttpRequest",
        }

    def _bearer_url(self, action: str) -> str:
        self._request_counter += 1
        origin = self._bearer.get("origin", self.owa_url)
        base_path = self._bearer.get("base_path", "/owa/service.svc")
        return f"{origin}{base_path}?action={action}&app=Mail&n={self._request_counter}"

    async def _async_execute_on_anchor(self, url: str, headers: dict, body: str | None, timeout: float) -> BrowserResponse:
        """Fire a fetch() from the already-settled anchor page - no new tab, no navigation.

        We read the response body inside the page's own JS via fetch().text()
        and pass it back through evaluate(), instead of Playwright's
        page.expect_response()/response.body() (which goes through a CDP
        getResponseBody call keyed on the network resource's lifetime). On
        the modern Outlook SPA (outlook.cloud.microsoft) that CDP call fails
        with "Response body is not available for a response that was
        navigated away from" even when the page's own URL never changes -
        confirmed by tracing framenavigated events across the request, none
        fire. The failure appears tied to how this SPA's service
        worker/network layer interacts with CDP's body buffering, not actual
        top-level navigation, so avoiding CDP body retrieval sidesteps it
        entirely.
        """
        page = self._anchor_page
        fetch_opts = {"method": "POST", "headers": headers, "credentials": "include"}
        if body is not None:
            fetch_opts["body"] = body

        script = """
            async ([url, opts]) => {
                try {
                    const r = await fetch(url, opts);
                    const text = await r.text();
                    const headers = {};
                    r.headers.forEach((v, k) => { headers[k] = v; });
                    return {status: r.status, headers, body: text, error: null};
                } catch (e) {
                    return {status: 0, headers: {}, body: '', error: String(e)};
                }
            }
            """

        # The modern Outlook SPA can navigate itself internally (route
        # changes with no visible URL/reload) at any point, not just right
        # after the reload() in _async_capture_bearer_context. If that
        # happens while page.evaluate() is mid-flight, Chromium tears down
        # the page's JS execution context out from under it - this is
        # unrelated to a real browser/context crash (see _CRASH_HINTS
        # above), so it doesn't need a full relaunch, just a brief wait for
        # the SPA to resettle and one retry of the same fetch.
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    page.evaluate(script, [url, fetch_opts]), timeout=timeout
                )
                break
            except Exception as exc:
                if attempt == 0 and "execution context" in str(exc).lower():
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    continue
                raise

        if result.get("error"):
            raise RuntimeError(f"fetch() from anchor page failed: {result['error']}")

        return BrowserResponse(
            result["status"], result["headers"], result["body"].encode("utf-8")
        )

    async def _async_execute(self, url: str, headers: dict, body: str | None, match_hint: str, timeout: float) -> BrowserResponse:
        page = await self._context.new_page()
        try:
            try:
                await page.goto(f"{self.owa_url}/owa/", wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception:
                pass

            fetch_opts = {"method": "POST", "headers": headers, "credentials": "same-origin"}
            if body is not None:
                fetch_opts["body"] = body

            async with page.expect_response(
                lambda r: match_hint in r.url, timeout=timeout * 1000
            ) as resp_info:
                await page.evaluate(
                    "([url, opts]) => fetch(url, opts).then(r => r.status).catch(() => -1)",
                    [url, fetch_opts],
                )
            response = await resp_info.value
            status = response.status
            resp_headers = await response.all_headers()
            content = await response.body()
            return BrowserResponse(status, resp_headers, content)
        finally:
            await page.close()

    async def _async_post_json(self, action: str, payload: dict, timeout: float) -> BrowserResponse:
        await self._async_ensure_context()
        await self._async_ensure_auth()
        body = _json.dumps(payload)

        if self._auth_mode == "bearer":
            url = self._bearer_url(action)
            headers = self._bearer_request_headers(action)
            return await self._async_execute_on_anchor(url, headers, body, timeout)

        canary = await self._async_current_canary()
        url = f"{self.owa_url}/owa/service.svc?action={action}&EP=1&ID=-1&AC=1"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Action": action,
            "X-OWA-CANARY": canary,
            "X-Requested-With": "XMLHttpRequest",
        }
        return await self._async_execute(url, headers, body, f"action={action}", timeout)

    async def _async_post_header_payload(self, action: str, payload: dict, timeout: float) -> BrowserResponse:
        await self._async_ensure_context()
        await self._async_ensure_auth()
        url_post_data = quote(_json.dumps(payload, separators=(",", ":")))

        if self._auth_mode == "bearer":
            url = self._bearer_url(action)
            headers = self._bearer_request_headers(action)
            headers["X-OWA-UrlPostData"] = url_post_data
            return await self._async_execute_on_anchor(url, headers, None, timeout)

        canary = await self._async_current_canary()
        url = f"{self.owa_url}/owa/service.svc?action={action}&EP=1&ID=-1&AC=1"
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Action": action,
            "X-OWA-CANARY": canary,
            "X-OWA-UrlPostData": url_post_data,
            "X-Requested-With": "XMLHttpRequest",
        }
        return await self._async_execute(url, headers, None, f"action={action}", timeout)

    def post_json(self, action: str, payload: dict, timeout: float = 30) -> BrowserResponse:
        return self._run_with_recovery(
            lambda: self._async_post_json(action, payload, timeout), timeout=timeout + 30
        )

    def post_header_payload(self, action: str, payload: dict, timeout: float = 30) -> BrowserResponse:
        return self._run_with_recovery(
            lambda: self._async_post_header_payload(action, payload, timeout), timeout=timeout + 30
        )

    async def _async_post_substrate(
        self, path_and_query: str, extra_headers: dict, payload: dict, timeout: float
    ) -> BrowserResponse:
        """POST to a modern-Outlook REST surface outside /owa/service.svc.

        The People app (outlook.cloud.microsoft/people) doesn't use EWS
        actions like ResolveNames at all - its search box hits
        /search/api/v1/suggestions and its contact-card expansion hits
        /PeopleGraphVx/v1.0/peopleLookup, both on the same origin and,
        confirmed by decoding the token, the same OAuth audience
        (aud=https://outlook.office.com) already captured for Mail actions
        by _async_capture_bearer_context - so no separate auth flow is
        needed, just different paths/headers. Only exists in bearer mode:
        classic canary-cookie OWA (on-prem, or a cloud tenant not yet on
        the modern backend) has no equivalent surface.
        """
        await self._async_ensure_context()
        await self._async_ensure_auth()
        if self._auth_mode != "bearer":
            raise BearerModeRequiredError(
                "This action requires the modern Outlook (bearer-auth) backend; "
                "not available on this tenant's classic OWA."
            )

        self._request_counter += 1
        origin = self._bearer.get("origin", self.owa_url)
        sep = "&" if "?" in path_and_query else "?"
        url = f"{origin}{path_and_query}{sep}n={self._request_counter}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._bearer.get("authorization", ""),
            "x-anchormailbox": self._bearer.get("x-anchormailbox", ""),
            "x-tenantid": self._bearer.get("x-tenantid", ""),
            "x-owa-sessionid": self._bearer.get("x-owa-sessionid", ""),
            "X-Requested-With": "XMLHttpRequest",
            **extra_headers,
        }
        body = _json.dumps(payload)
        return await self._async_execute_on_anchor(url, headers, body, timeout)

    def post_substrate(
        self, path_and_query: str, extra_headers: dict, payload: dict, timeout: float = 30
    ) -> BrowserResponse:
        return self._run_with_recovery(
            lambda: self._async_post_substrate(path_and_query, extra_headers, payload, timeout),
            timeout=timeout + 30,
        )

    # ------------------------------------------------------------------
    # Copilot (chat pane UI automation - no documented API exists)
    # ------------------------------------------------------------------
    #
    # Everything below drives Copilot's actual chat pane DOM instead of a JSON
    # action - Copilot has no service.svc equivalent, and discovery capture
    # 20260911-112708-e917 confirmed there is no HTTP endpoint carrying either
    # the prompt or the generated answer (see the Copilot notes in
    # PROJECT_STATUS.md for what that capture did and didn't settle).
    #
    # The load-bearing fact, and the reason the first implementation failed
    # every live test: **the chat pane is a cross-origin iframe.** OWA runs on
    # the mailbox host, the pane is served from _COPILOT_FRAME_HOST_HINTS. A
    # Playwright `page.locator(...)` only ever searches the main frame, so the
    # original `role=complementary` / `[class*="Copilot"]` queries could not
    # match no matter how well guessed - the nodes are in another frame tree.
    # Everything here therefore resolves the *frame* first and roots every
    # subsequent query inside it.
    #
    # The launch button is the one part that *is* main-frame OWA chrome, and
    # matching it on the accessible name "Copilot" works because Microsoft
    # doesn't translate the brand name. The prompts inside the pane are
    # localised - see _COPILOT_STOP_HINTS.

    async def _async_copilot_frame(self, page, timeout: float):
        """Return a *live* Copilot iframe Frame, or None if none has appeared yet.

        Polls instead of matching once: the iframe is created after the
        launcher click and its document load is a separate navigation, so it
        can be attached-but-blank for a moment.

        The `is_detached()` check is not defensive padding - it is the fix for
        the second live failure mode this module hit. Every grounded tool calls
        `page.goto(nav_url)` first, which tears down the previous call's Copilot
        iframe, but a detached Frame stays in `page.frames` for a while and its
        `.url` still matches. Matching on URL alone therefore handed back the
        dead frame from the *previous* tool call, and the run failed with
        "Locator.wait_for: Frame was detached" on every second call - or, when
        the corpse still had a body element, with "no textbox to type into",
        which looks like a completely different (and much more alarming) bug.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for frame in page.frames:
                url = (frame.url or "").lower()
                if not any(hint in url for hint in _COPILOT_FRAME_HOST_HINTS):
                    continue
                try:
                    if frame.is_detached():
                        continue
                except Exception:
                    continue
                return frame
            await asyncio.sleep(0.5)
        return None

    async def _async_copilot_locate_pane(self, page, timeout: float = 10):
        """Locate the pane root as a locator *inside* the Copilot iframe.

        Returns a `body`-rooted locator rather than the Frame itself so that
        _async_copilot_submit / _async_copilot_wait_and_read can keep using
        ordinary locator methods (`get_by_role`, `inner_text`, `count`) -
        Frame's API differs just enough to matter (`Frame.inner_text` requires
        a selector argument).
        """
        frame = await self._async_copilot_frame(page, timeout)
        if frame is None:
            return None
        return frame.locator("body")

    @staticmethod
    async def _async_copilot_pane_usable(pane) -> bool:
        """Is this pane locator backed by a live frame with a composer in it?

        The fast path below skips the launcher click when a pane is already
        open, and it used to accept any pane whose body existed. That is not
        enough: a frame torn down by the grounding navigation can still answer
        `count()` while being useless, so the skip has to be justified by the
        one thing the caller actually needs next - somewhere to type.
        """
        try:
            if not await pane.count():
                return False
            return await pane.get_by_role("textbox").count() > 0
        except Exception:
            return False

    async def _async_copilot_open_pane(self, page, timeout: float):
        """Open the Copilot pane and return a locator rooted in a *usable* frame.

        "Usable" (a live frame containing a composer) rather than merely
        "present", and reached by polling rather than one resolve, because
        Copilot's pane is created and then **replaced** during its own load: the
        iframe that exists a moment after the launcher click is not the one that
        ends up serving the chat. Resolving once and trusting it produced two
        different-looking live failures from that single race - a
        `Locator.wait_for: Frame was detached` when the first frame died while
        being waited on, and a "no textbox to type into" when the replacement
        had not rendered its composer yet. Neither was a selector problem, which
        is why guessing at better selectors could not have fixed them.
        """
        pane = await self._async_copilot_locate_pane(page, timeout=1)
        if pane is not None and await self._async_copilot_pane_usable(pane):
            return pane

        launcher = page.get_by_role("button", name=re.compile("copilot", re.I))
        if await launcher.count() == 0:
            raise RuntimeError(
                "Could not find a Copilot launch button on the current page "
                f"({page.url}) - the page may not offer Copilot at all. Note "
                "that a calendar *item* page has no launcher; grounding an "
                "event goes via the calendar view (see _async_copilot_ask)."
            )
        await launcher.first.click(timeout=timeout * 1000)

        deadline = time.time() + timeout
        last_pane = None
        while time.time() < deadline:
            pane = await self._async_copilot_locate_pane(page, timeout=2)
            if pane is not None:
                last_pane = pane
                if await self._async_copilot_pane_usable(pane):
                    return pane
            await asyncio.sleep(0.5)

        if last_pane is None:
            hosts = ", ".join(_COPILOT_FRAME_HOST_HINTS)
            raise RuntimeError(
                "Clicked the Copilot launcher but no Copilot iframe appeared "
                f"within {timeout}s (looked for a live frame whose URL contains "
                f"one of: {hosts}). Either the pane host changed - update "
                "_COPILOT_FRAME_HOST_HINTS - or the pane failed to load."
            )

        # A frame kept appearing but never offered anywhere to type. That is the
        # one case where "the side panel only has preset prompt chips on this
        # tenant" is a real possibility rather than a race, so hand back the
        # structure needed to write a chip-clicking path instead of a bare
        # timeout.
        structure = await self._async_copilot_pane_structure(last_pane)
        raise RuntimeError(
            f"A Copilot iframe was present on {page.url} but never offered a "
            f"textbox within {timeout}s. If this is reproducible the pane may "
            f"only offer preset prompt chips here, and a chip-clicking path "
            f"belongs in _async_copilot_submit. Pane structure "
            f"(content-free): {structure}"
        )

    async def _async_copilot_submit(self, pane, prompt: str, timeout: float) -> None:
        """Type a free-text prompt into the pane's input box and send it.

        The discovery capture only ever recorded the user clicking Copilot's
        *suggested prompt chips*, so the presence of a free-text box in the
        side panel is inferred, not observed. If it turns out there isn't one,
        this is where a chip-clicking path would go - hence the explicit error
        rather than a bare Playwright timeout.
        """
        input_box = pane.get_by_role("textbox").first
        if await input_box.count() == 0:
            raise RuntimeError(
                "Copilot's iframe was found but it has no textbox to type into. "
                "The side panel may only offer preset prompt chips on this "
                "tenant - a chip-clicking path belongs in _async_copilot_submit."
            )
        await input_box.wait_for(state="visible", timeout=timeout * 1000)
        await input_box.fill(prompt)
        await input_box.press("Enter")

    async def _async_copilot_acquire_pane(self, page, launcher_fallback_url: str | None):
        """Open the pane, retrying the launcher elsewhere if this page has none.

        Not every page that can *display* an item also offers a Copilot
        launcher: the discovery capture showed a calendar item page with no
        launcher at all, and the user reaching meeting prep from the calendar
        view instead. Navigating to the item is still what selects it, so the
        grounding navigation stands and only the launcher hunt moves.
        """
        try:
            return await self._async_copilot_open_pane(page, timeout=10)
        except RuntimeError:
            if not launcher_fallback_url:
                raise
            await page.goto(launcher_fallback_url, wait_until="networkidle", timeout=15000)
            return await self._async_copilot_open_pane(page, timeout=10)

    async def _async_copilot_open_and_submit(self, page, prompt: str, launcher_fallback_url):
        """Get the pane and the prompt into it, surviving an iframe swap.

        Returns `(pane, baseline)`. These three steps retry as one unit because
        they are one unit: when the frame is replaced, the pane locator *and*
        the baseline text both belonged to the frame that just died, so keeping
        either across a retry is how you end up diffing an answer against a
        stale snapshot.

        The interesting case is the last one guarded here. If a previous
        attempt's `Enter` actually landed and the frame died immediately after,
        retrying would ask Copilot the same question twice - visible to the user
        as a duplicated turn, and wasteful of a slow generation. Comparing the
        fresh transcript's turn count against the one we typed into detects
        that, and in that case we keep the *earlier* baseline: the new one
        already contains the answer being generated, and using it would make
        `_copilot_answer_text` treat the answer as pre-existing furniture and
        subtract it.
        """
        submitted_markers: int | None = None
        submitted_baseline = ""

        for attempt in range(1, _COPILOT_SUBMIT_ATTEMPTS + 1):
            try:
                pane = await self._async_copilot_acquire_pane(page, launcher_fallback_url)

                # Snapshot before submitting: this is what tells
                # _async_copilot_wait_and_read the difference between Copilot's
                # answer and Copilot's own UI, and between a real timeout and a
                # prompt that never landed.
                try:
                    baseline = (await pane.inner_text()).strip()
                except Exception:
                    baseline = ""

                if (
                    submitted_markers is not None
                    and _copilot_answer_marker_count(baseline) > submitted_markers
                ):
                    return pane, submitted_baseline

                submitted_markers = _copilot_answer_marker_count(baseline)
                submitted_baseline = baseline
                await self._async_copilot_submit(pane, prompt, timeout=10)
                return pane, baseline

            except Exception as exc:
                if attempt >= _COPILOT_SUBMIT_ATTEMPTS or not _copilot_is_frame_swap(exc):
                    raise
                # Let the replacement frame render before resolving it again;
                # retrying instantly just finds the same half-built pane.
                await asyncio.sleep(_COPILOT_FRAME_SWAP_PAUSE)

        raise RuntimeError(  # pragma: no cover - loop either returns or raises
            "Copilot's iframe was replaced on every attempt to submit the prompt "
            f"({_COPILOT_SUBMIT_ATTEMPTS} tries)."
        )

    async def _async_copilot_pane_structure(self, pane, limit: int = 40) -> list[dict]:
        """A content-free structural sketch of the pane, for a no-response run.

        Records tag / role / test-id / class and text *length* - never text. A
        Copilot answer is mailbox-derived content and this ends up in a
        smoke-test log; the discovery recorder draws exactly the same line
        (tag/role/label, never values). aria-label is recorded only for the
        interactive roles in _COPILOT_LABEL_SAFE_ROLES, where it is an
        affordance name rather than a message.

        This exists because every remaining unknown in this module - is there a
        free-text box, is there a Coaching affordance, which node holds the
        answer - is a question about the pane's DOM that guessing has already
        failed to answer once.
        """
        script = """
        (root, args) => {
          const out = [];
          const safe = new Set(args.safeRoles);
          const walk = (el, depth) => {
            if (out.length >= args.limit) return;
            const role = el.getAttribute('role');
            const tid = el.getAttribute('data-testid') || el.getAttribute('data-test-id');
            const label = el.getAttribute('aria-label');
            const tag = el.tagName.toLowerCase();
            const interactive = tag === 'textarea' || tag === 'input' || tag === 'button';
            if (role || tid || label || interactive) {
              const entry = {
                depth: depth,
                tag: tag,
                role: role || null,
                testid: tid || null,
                text_len: (el.innerText || '').trim().length,
                cls: (el.className || '').toString().slice(0, 60) || null,
              };
              if (label) {
                entry.label = safe.has(role || '') || interactive
                  ? label.slice(0, 60)
                  : '<omitted: non-interactive role>';
              }
              out.push(entry);
            }
            for (const child of el.children) walk(child, depth + 1);
          };
          walk(root, 0);
          return out;
        }
        """
        try:
            return await pane.evaluate(
                script,
                {"limit": limit, "safeRoles": sorted(_COPILOT_LABEL_SAFE_ROLES)},
            )
        except Exception as exc:
            return [{"error": f"could not read pane structure: {exc}"}]

    @staticmethod
    async def _async_copilot_button_labels(pane) -> set[str]:
        """Visible button labels in the pane, as chrome for _copilot_answer_text.

        Read *after* generation settles, because the follow-up suggestion chips
        Copilot appends to an answer only exist by then - and they are the reason
        this is needed: their wording is generated per answer, so no hint table
        can cover them, but "it is a button" always holds.
        """
        try:
            return {t.strip() for t in await pane.get_by_role("button").all_inner_texts() if t.strip()}
        except Exception:
            return set()

    async def _async_copilot_wait_and_read(
        self, pane, timeout: float, baseline: str = "", prompt: str = ""
    ) -> dict:
        """Poll the pane until generation settles, a rate-limit banner appears, or timeout.

        Settling requires all of: the pane's text differs from `baseline`, no
        visible "Stop generating"-style control, the text unchanged for N
        consecutive polls, and the pane not showing an in-flight status line.

        `baseline` - the pane's text from *before* the prompt was submitted - is
        what makes this verdict trustworthy, and it was missing. The pane's own
        greeting and prompt chips are already non-empty and already stable, so
        with no visible stop button (generation takes a moment to start) the
        third poll returned that chrome as {"status": "ok"} about three seconds
        in. That is worse than a failure: it would have marked #901-905 verified
        while Copilot had not answered at all.

        **`baseline` alone was not enough**, and the way it failed is the reason
        for the marker/progress conditions below. Re-running the smoke suite on
        2026-09-15 produced `{"status": "ok"}` whose text was
        `"In corso…\\nScarica l'app per dispositivi mobili Copilot\\n…"` - a
        progress line plus an app-promo block. Both defences were blind to it for
        the same structural reason: the block appears *after* the baseline
        snapshot, so subtracting the baseline cannot remove it, and **a progress
        indicator is by construction both new and unchanging**, so waiting for
        stability cannot reject it. `_COPILOT_STOP_HINTS` should have caught it,
        but no stop control matched during that phase.

        **The primary signal is now `aria-busy`, not text at all**, because a
        third version of this bug was found the same way as the first two and no
        amount of text analysis was going to end that sequence. A 444-sample DOM
        probe of a live generation (see `_COPILOT_BUSY_SELECTOR`) showed
        `aria-busy="true"` covering the generating window *exactly*, while the
        text went flat at 377 chars for 27 consecutive polls (~14s) with the real
        8836-char answer still 46 seconds away. Text stability would have
        returned that fragment confidently; `aria-busy` would not have.

        So the rule is: **once we have seen `aria-busy` in this call, its
        clearing is what "finished" means.** Text stability is still required
        afterwards, because the probe measured a ~1.4s lag between the flag
        clearing and the final text landing - the flag says "stopped
        generating", not "the DOM has caught up".

        The text heuristics remain as a *fallback* for a pane that never exposes
        `aria-busy` at all (an unlisted redesign), and only then. They are, in
        order of trust:

        - **A new turn marker appeared** (count, not presence - `saw_busy` aside,
          the pane is reused across calls and keeps its history, so presence is
          true from a previous answer before this one starts) and no progress
          line: settle after `_COPILOT_SETTLE_POLLS`.
        - **Neither** - demand `_COPILOT_SETTLE_POLLS_NO_MARKER`, the weakest
          evidence getting the longest wait.

        Every stale table therefore degrades to `status: "timeout"` with real
        `partial_text`, which is honest, rather than to a confident wrong answer.
        """
        deadline = time.time() + timeout
        baseline = (baseline or "").strip()
        base_markers = _copilot_answer_marker_count(baseline)
        last_text = ""
        stable_polls = 0
        saw_change = False
        saw_busy = False
        stop_button = pane.get_by_role(
            "button", name=re.compile("|".join(_COPILOT_STOP_HINTS), re.I)
        )
        busy_flag = pane.locator(_COPILOT_BUSY_SELECTOR)

        while time.time() < deadline:
            text = (await pane.inner_text()).strip()
            lowered = text.lower()

            if any(hint in lowered for hint in _COPILOT_RATE_LIMIT_HINTS):
                raise CopilotUnavailableError("Copilot reported a capacity/rate-limit condition.")
            if any(hint in lowered for hint in _COPILOT_SIGNIN_HINTS):
                raise SessionExpiredError("Copilot pane shows a sign-in prompt; session likely expired.")

            if text and text != baseline:
                saw_change = True

            busy = await busy_flag.count() > 0
            if busy:
                saw_busy = True

            if saw_busy:
                # The DOM told us generation started, so its clearing is the
                # verdict; `still_generating` below is what holds us until then.
                ready = True
                settled_enough = _COPILOT_SETTLE_POLLS
            else:
                # No aria-busy on this pane - fall back to reading the text.
                new_turn = _copilot_answer_marker_count(text) > base_markers
                ready = new_turn and not _copilot_has_progress_line(text)
                settled_enough = (
                    _COPILOT_SETTLE_POLLS if new_turn else _COPILOT_SETTLE_POLLS_NO_MARKER
                )

            still_generating = busy or await stop_button.count() > 0
            if saw_change and ready and not still_generating and text and text == last_text:
                stable_polls += 1
                if stable_polls >= settled_enough:
                    return {
                        "status": "ok",
                        "text": _copilot_answer_text(
                            baseline, text, prompt,
                            await self._async_copilot_button_labels(pane),
                        ),
                    }
            else:
                stable_polls = 0

            last_text = text
            await asyncio.sleep(_COPILOT_POLL_SECONDS)

        if not saw_change:
            # The pane never changed, so there is no partial answer to hand back.
            # Reporting one as {"status": "timeout", "partial_text": <chrome>}
            # would return Copilot's UI as a result - and the smoke test counts
            # a non-empty partial_text as a pass, so this has to be its own
            # status, with the structure needed to fix it.
            return {
                "status": "no_response",
                "error": (
                    f"The Copilot pane was reached and the prompt was submitted, but "
                    f"nothing in the pane changed within {timeout:.0f}s. Either the "
                    f"prompt never actually reached Copilot (the textbox found by "
                    f"_async_copilot_submit may not be the composer) or the answer "
                    f"renders somewhere pane.inner_text() does not see. "
                    f"pane_structure below is a content-free sketch for writing a "
                    f"real selector."
                ),
                "pane_structure": await self._async_copilot_pane_structure(pane),
            }

        return {
            "status": "timeout",
            "partial_text": _copilot_answer_text(
                baseline, last_text, prompt,
                await self._async_copilot_button_labels(pane),
            ),
        }

    async def _async_copilot_ask(
        self, prompt: str, nav_url: str | None, timeout: float, launcher_fallback_url: str | None = None
    ) -> dict:
        await self._async_ensure_context()
        await self._async_ensure_auth()
        if self._auth_mode != "bearer":
            raise BearerModeRequiredError(
                "Copilot requires the modern Outlook (bearer-auth) backend; "
                "not available on this tenant's classic OWA."
            )

        if self._copilot_lock is None:
            self._copilot_lock = asyncio.Lock()

        async with self._copilot_lock:
            page = self._anchor_page
            if nav_url:
                try:
                    await page.goto(nav_url, wait_until="networkidle", timeout=15000)
                except Exception:
                    pass  # grounding is best-effort - fall through and ask ungrounded

            pane, baseline = await self._async_copilot_open_and_submit(
                page, prompt, launcher_fallback_url
            )
            return await self._async_copilot_wait_and_read(
                pane, timeout=timeout, baseline=baseline, prompt=prompt
            )

    def copilot_ask(
        self,
        prompt: str,
        *,
        nav_url: str | None = None,
        launcher_fallback_url: str | None = None,
        timeout: float = 90,
    ) -> dict:
        """Ask Copilot a question via its chat pane. See _async_copilot_ask for caveats.

        `launcher_fallback_url` is where to retry if `nav_url` turns out to be a
        page with no Copilot launcher (calendar item pages are one such).

        `timeout` budgets the *generation*; getting the prompt in can itself cost
        time when the iframe swaps mid-submit (see
        `_async_copilot_open_and_submit`), so `_COPILOT_ACQUIRE_BUDGET` is added
        on top rather than letting a retried submit eat the caller's answer time.

        Returns {"status": "ok", "text": ...} or {"status": "timeout", "partial_text": ...}.
        Raises BearerModeRequiredError, SessionExpiredError, or CopilotUnavailableError.
        """
        return self._run_with_recovery(
            lambda: self._async_copilot_ask(prompt, nav_url, timeout, launcher_fallback_url),
            timeout=timeout + 30 + _COPILOT_ACQUIRE_BUDGET,
        )

    # ------------------------------------------------------------------
    # Binary download (attachments)
    # ------------------------------------------------------------------

    async def _async_download_attachment(self, attachment_id: str, timeout: float) -> BrowserResponse:
        await self._async_ensure_context()
        canary = await self._async_current_canary()
        url = (
            f"{self.owa_url}/owa/service.svc/s/GetFileAttachment"
            f"?id={quote(attachment_id)}&X-OWA-CANARY={quote(canary)}"
        )
        page = await self._context.new_page()
        try:
            try:
                await page.goto(f"{self.owa_url}/owa/", wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception:
                pass

            async with page.expect_response(
                lambda r: "GetFileAttachment" in r.url, timeout=timeout * 1000
            ) as resp_info:
                await page.evaluate(
                    "(url) => fetch(url, {credentials: 'same-origin'}).then(r => r.status).catch(() => -1)",
                    url,
                )
            response = await resp_info.value
            status = response.status
            resp_headers = await response.all_headers()
            content = await response.body()
            return BrowserResponse(status, resp_headers, content)
        finally:
            await page.close()

    def download_attachment(self, attachment_id: str, timeout: float = 60) -> BrowserResponse:
        return self._run_with_recovery(
            lambda: self._async_download_attachment(attachment_id, timeout), timeout=timeout + 30
        )
