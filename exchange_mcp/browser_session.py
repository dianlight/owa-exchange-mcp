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

# Where the persistent Chromium profile lives when EXCHANGE_BROWSER_PROFILE_DIR
# isn't set. An installed package must not write into site-packages, so it uses
# a stable per-user location; a source checkout keeps its profile in the repo so
# a developer's signed-in session and their working tree stay together.
_INSTALLED_PROFILE_DIR = Path.home() / "owa-mcp" / ".browser-profile"


def default_profile_dir() -> Path:
    """Resolve the default profile directory for this deployment.

    A source checkout is detected by pyproject.toml sitting next to the package
    directory - true for `python -m exchange_mcp.server` from the repo, false for
    the installed `exchange-mcp-server` console script (whose package lives in
    site-packages, where writing a browser profile would be wrong and often
    unwritable).
    """
    package_parent = Path(__file__).resolve().parent.parent
    if (package_parent / "pyproject.toml").exists():
        return package_parent / ".browser-profile"
    return _INSTALLED_PROFILE_DIR


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
        """Run a coroutine; if the browser/context died, relaunch and retry once."""
        try:
            return self._run(coro_factory(), timeout=timeout)
        except Exception as exc:
            msg = str(exc).lower()
            if any(hint in msg for hint in _CRASH_HINTS):
                self._context_closed = True
                self._run(self._async_ensure_context(), timeout=120)
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

    async def _async_ensure_context(self) -> None:
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()

        async with self._launch_lock:
            if not self._context_closed and self._context is not None:
                return

            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            self.profile_dir.mkdir(parents=True, exist_ok=True)

            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1280, "height": 900},
            )
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
        await asyncio.sleep(1)
        self.headless = headless
        await self._async_ensure_context()

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
    # Everything below drives Copilot's actual chat pane DOM inside the
    # already-rendered anchor page, instead of a JSON action - Copilot has
    # no service.svc equivalent. The selectors are best-effort guesses based
    # on Fluent UI ARIA conventions used elsewhere in the modern Outlook web
    # client (role-based queries, since Fluent UI consistently annotates
    # interactive elements with accessible names/roles) and have NOT been
    # confirmed against a live Copilot pane. Run the discovery spike
    # (--show-browser, inspect the real DOM) and correct these methods -
    # they're deliberately the only place selector knowledge lives, so a
    # correction only has to happen here.

    async def _async_copilot_locate_pane(self, page):
        pane = page.get_by_role("complementary", name=re.compile("copilot", re.I))
        if await pane.count() == 0:
            pane = page.locator('[class*="Copilot" i][role]').first
        return pane

    async def _async_copilot_open_pane(self, page, timeout: float):
        pane = await self._async_copilot_locate_pane(page)
        if await pane.count() and await pane.first.is_visible():
            return pane.first

        launcher = page.get_by_role("button", name=re.compile("copilot", re.I))
        if await launcher.count() == 0:
            raise RuntimeError(
                "Could not find a Copilot launch button on the current page - "
                "selectors need updating (see discovery spike notes)."
            )
        await launcher.first.click(timeout=timeout * 1000)

        pane = await self._async_copilot_locate_pane(page)
        await pane.first.wait_for(state="visible", timeout=timeout * 1000)
        return pane.first

    async def _async_copilot_submit(self, pane, prompt: str, timeout: float) -> None:
        input_box = pane.get_by_role("textbox").first
        await input_box.wait_for(state="visible", timeout=timeout * 1000)
        await input_box.fill(prompt)
        await input_box.press("Enter")

    async def _async_copilot_wait_and_read(self, pane, timeout: float) -> dict:
        """Poll the pane until generation settles, a rate-limit banner appears, or timeout.

        "Settled" is approximated as: no visible "Stop generating"-style
        control, and the pane's text hasn't changed since the last poll -
        a real generation-complete DOM signal (data-* state attribute, etc.)
        should replace this once the spike identifies one; text-stability
        polling is a reasonable but slower fallback.
        """
        deadline = time.time() + timeout
        last_text = ""
        stop_button = pane.get_by_role("button", name=re.compile("stop", re.I))

        while time.time() < deadline:
            text = (await pane.inner_text()).strip()
            lowered = text.lower()

            if any(hint in lowered for hint in _COPILOT_RATE_LIMIT_HINTS):
                raise CopilotUnavailableError("Copilot reported a capacity/rate-limit condition.")
            if any(hint in lowered for hint in _COPILOT_SIGNIN_HINTS):
                raise SessionExpiredError("Copilot pane shows a sign-in prompt; session likely expired.")

            still_generating = await stop_button.count() > 0
            if not still_generating and text and text == last_text:
                return {"status": "ok", "text": text}

            last_text = text
            await asyncio.sleep(1)

        return {"status": "timeout", "partial_text": last_text}

    async def _async_copilot_ask(self, prompt: str, nav_url: str | None, timeout: float) -> dict:
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

            pane = await self._async_copilot_open_pane(page, timeout=10)
            await self._async_copilot_submit(pane, prompt, timeout=10)
            return await self._async_copilot_wait_and_read(pane, timeout=timeout)

    def copilot_ask(self, prompt: str, *, nav_url: str | None = None, timeout: float = 90) -> dict:
        """Ask Copilot a question via its chat pane. See _async_copilot_ask for caveats.

        Returns {"status": "ok", "text": ...} or {"status": "timeout", "partial_text": ...}.
        Raises BearerModeRequiredError, SessionExpiredError, or CopilotUnavailableError.
        """
        return self._run_with_recovery(
            lambda: self._async_copilot_ask(prompt, nav_url, timeout), timeout=timeout + 30
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
