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
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse

_CRASH_HINTS = (
    "target closed",
    "browser has been closed",
    "connection closed",
    "has been closed",
)


class SessionExpiredError(Exception):
    """Raised when the OWA session has expired (HTTP 401/440 or HTML redirect)."""


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
        self.headless = headless
        self.profile_dir = Path(profile_dir) if profile_dir else (
            Path(__file__).parent.parent / ".browser-profile"
        )

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="owa-browser")
        self._thread.start()

        self._playwright = None
        self._context = None
        self._anchor_page = None
        self._context_closed = True  # forces a launch on first use
        self._launch_lock: asyncio.Lock | None = None  # created lazily, on the browser loop

        self._cached_username: str | None = None
        self._cached_password: str | None = None

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

    @property
    def auth_mode(self) -> str:
        """"canary" (classic cookie CSRF token) or "bearer" (OAuth JWT, modern Outlook)."""
        return self._auth_mode

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
                pass  # ensure_logged_in() will retry navigation as needed

    def _on_context_closed(self) -> None:
        self._context_closed = True

    async def _async_shutdown(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
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
        if await self._async_is_session_valid():
            return True
        return await self._async_capture_bearer_context()

    async def _async_ensure_logged_in(self, username: str | None, password: str | None) -> dict:
        await self._async_ensure_context()
        page = self._anchor_page
        try:
            await page.goto(f"{self.owa_url}/owa/", wait_until="networkidle", timeout=30000)
        except Exception:
            pass

        # On the modern Outlook backend, the *first* top-level navigation to
        # /owa/ on a freshly-launched page reliably lands on an interactive
        # login/account-selection redirect even with a perfectly valid SSO
        # session - it's only a *second* touch of the page (the reload()
        # inside _async_capture_bearer_context, below) that lets the SPA
        # silently reacquire a token. So the goto above is required warm-up,
        # not something to skip even when we suspect we're already logged in.
        if await self._async_has_active_session():
            return {"success": True, "message": "Session already active."}

        username = username or self._cached_username
        password = password or self._cached_password
        if not username or not password:
            return {
                "success": False,
                "error": "No active session and no credentials available to log in.",
            }

        result = await self._async_run_login_flow(page, username, password)
        if result.get("success"):
            self._cached_username = username
            self._cached_password = password
        return result

    async def _async_run_login_flow(self, page, username: str, password: str) -> dict:
        """Same interactive flow already debugged in login.py, run on the anchor tab."""
        try:
            await page.fill('input[name="loginfmt"]', username)
            try:
                await page.click("#idSIButton9", timeout=5000)
            except Exception:
                await page.press('input[name="loginfmt"]', "Enter")
            await page.wait_for_load_state("networkidle")

            await page.wait_for_selector('input[name="passwd"]', state="visible", timeout=15000)
            await page.fill('input[name="passwd"]', password)
            try:
                await page.click("#idSIButton9", timeout=5000)
            except Exception:
                await page.press('input[name="passwd"]', "Enter")
            await page.wait_for_load_state("networkidle")

            kmsi_handled = False
            for _ in range(90):  # wait up to 90s for mobile MFA approval
                await asyncio.sleep(1)
                try:
                    url = page.url

                    if urlparse(url).netloc == self.owa_host and "ofam" not in url and "adfs" not in url:
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        for _ in range(15):
                            if await self._async_is_session_valid():
                                break
                            await asyncio.sleep(1)
                        return {"success": True, "message": "Login successful."}

                    if not kmsi_handled:
                        try:
                            if (
                                await page.locator('input[name="passwd"]').count() == 0
                                and await page.locator("#idSIButton9").is_visible(timeout=1000)
                            ):
                                await page.click("#idSIButton9", timeout=5000)
                                kmsi_handled = True
                                await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                except Exception as e:
                    err = str(e).lower()
                    if any(k in err for k in ("navigation", "destroyed", "target closed")):
                        try:
                            await page.wait_for_load_state("load", timeout=15000)
                            if urlparse(page.url).netloc == self.owa_host and "ofam" not in page.url:
                                return {"success": True, "message": "Login successful."}
                        except Exception:
                            pass

            return {"success": False, "error": "2FA approval not received within 90 seconds."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def ensure_logged_in(self, username: str | None = None, password: str | None = None, timeout: float = 120) -> dict:
        return self._run_with_recovery(
            lambda: self._async_ensure_logged_in(username, password), timeout=timeout
        )

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

        result = await asyncio.wait_for(
            page.evaluate(
                """
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
                """,
                [url, fetch_opts],
            ),
            timeout=timeout,
        )
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
