"""Record a live, user-driven OWA session so its API surface can be classified.

This is the capture half of capability discovery; [capability_classify.py](
capability_classify.py) is the analysis half. A recorder opens **its own**
Chromium on a **throwaway profile**, hands the window to the user, and writes
down every API call and UI action until they close it.

**Why a separate browser instead of the shared `BrowserSession`.** That
singleton owns the signed-in profile every tool's transport rides on. Driving
it interactively would mean navigating the anchor page out from under
in-flight calls, and recording on it would mean the capture is polluted by
this server's own traffic — the exact traffic we want to treat as "already
implemented". A discovery session is therefore a second, independent context
that shares nothing but the `owa_url`, built on the same
loop-on-a-background-thread pattern (see `BrowserSession`'s module docstring)
so callers get plain synchronous methods.

**Why a temporary profile.** A clean profile guarantees the capture starts
from a genuine cold sign-in and can't inherit cookies, cached SPA state, or
feature flags from the working profile — and it guarantees the reverse too:
nothing done in a discovery session can corrupt the profile the server serves
from. The cost is real and unavoidable: **the user has to sign in inside the
recording window**, every time.

That cost drives the safety rules below, which are requirements rather than
polish, because a capture that runs through a sign-in *will* see credential
traffic:

- **Requests to sign-in hosts are dropped entirely** (`_LOGIN_HOST_HINTS`) —
  not filtered later, never written.
- **`Authorization`, `Cookie` and canary headers are never recorded.** Only
  the allowlist in `_HEADER_ALLOWLIST` reaches disk.
- **Response bodies are stored as a content-free shape** (keys and types, see
  `json_shape`) unless the caller explicitly opts into raw bodies. Request
  bodies *are* stored in full: they are the API contract being discovered, and
  there is no way to propose an implementation without them.
- **Captured UI events never include input values** — only tag, role,
  accessible name and visible text (see `_INIT_SCRIPT`).

Even so, a capture directory holds real mailbox metadata. The default
locations are gitignored; `.discovery-sessions/` is not something to commit.

**No Playwright trace.** `context.tracing.stop()` has to run while the
context is alive, and the normal end of a discovery session is precisely the
user closing the last window — so the trace could only ever be written on the
abnormal path. The JSONL capture plus the UI-action log cover the same ground
without a failure mode that only bites on the happy path.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from exchange_mcp.browser_session import is_source_checkout
from exchange_mcp.capability_classify import json_shape

# Hosts that handle authentication. Requests to these are never recorded at
# all - a discovery session always starts with a real sign-in (fresh profile),
# so this is the difference between a capture file and a credential leak.
_LOGIN_HOST_HINTS = (
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.live.com",
    "login.windows.net",
    "sts.windows.net",
    "b2clogin.com",
    "adfs",
    "msauth",
    "msftauth",
    "aadcdn",
    "duosecurity",
    "okta",
    "ping-identity",
    "pingidentity",
)

# Hosts worth recording at all. An OWA page pulls from a dozen CDNs; only the
# Microsoft service origins can carry a mailbox API. The session's own OWA host
# is added at runtime, so on-prem deployments on a private domain work too.
_SERVICE_HOST_HINTS = (
    "outlook.office.com",
    "outlook.office365.com",
    "outlook.com",
    "cloud.microsoft",
    "office.com",
    "office365.com",
    "microsoft.com",
    "sharepoint.com",
    "substrate.office.com",
)

# Telemetry, beacons and asset pipelines: high-volume, zero API surface.
_NOISE_PATH_HINTS = (
    "/aria",
    "/collector",
    "/beacon",
    "/telemetry",
    "/owamailboxtelemetry",
    "/clientanalytics",
    "/tracking",
    "/logging",
    "/dsp/",
    "/bundles/",
    "/scripts/",
    "/resource.ashx",
    "/prefetch",
    "/ping",
    "favicon",
    "hostedcontent",
    "/serviceworker",
    "browser.pipe",
    "/rp/",
)

# Only these resource types can be an API call. Everything else (scripts,
# stylesheets, fonts, images) is counted as noise and dropped.
_API_RESOURCE_TYPES = ("fetch", "xhr")

# Request headers safe and useful to record. Anything not listed - crucially
# `authorization`, `cookie`, `x-owa-canary` - never reaches disk.
_HEADER_ALLOWLIST = (
    "action",
    "content-type",
    "x-owa-actionsource",
    "x-req-source",
    "prefer",
    "x-anchormailbox",  # mailbox address, not a secret; needed to reproduce a call
    "x-ms-endpoint-version",
    "scenario",
    "client-request-id",
)

# How much of a raw response body to keep when the caller opts into them, and
# how much of a request body to parse. Generous enough for a real EWS payload,
# small enough that a capture directory stays a few megabytes.
_MAX_BODY_BYTES = 512 * 1024

_JS_BINDING = "__owaDiscoveryRecord"

# Injected into every document in the recording context. Deliberately records
# no input values: tag, role, accessible name and visible text only, which is
# enough to say "the user opened Settings > Rules" and nothing more.
_INIT_SCRIPT = """
(() => {
  if (window.__owaDiscoveryInstalled) return;
  window.__owaDiscoveryInstalled = true;
  const describe = (el) => {
    if (!el || !el.tagName) return {};
    const attr = (n) => (el.getAttribute ? (el.getAttribute(n) || '') : '');
    const text = ((el.innerText || el.textContent || '') + '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    return {
      tag: el.tagName.toLowerCase(),
      role: attr('role'),
      label: attr('aria-label') || attr('title') || attr('placeholder') || text,
      elid: el.id || '',
      testid: attr('data-testid') || attr('data-automationid') || attr('data-automation-id') || '',
      text: text
    };
  };
  const send = (kind, el) => {
    try { window.__BINDING__(Object.assign({kind: kind, url: location.href}, describe(el))); }
    catch (e) { /* binding gone (page tearing down) - nothing to do */ }
  };
  document.addEventListener('click', (e) => send('click', e.target), true);
  document.addEventListener('change', (e) => send('change', e.target), true);
  document.addEventListener('submit', (e) => send('submit', e.target), true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === 'Escape') send('key:' + e.key, e.target);
  }, true);
})();
""".replace("__BINDING__", _JS_BINDING)


def default_discovery_dir() -> Path:
    """Where capture directories live.

    Mirrors `browser_session.default_profile_dir()`'s split for the same
    reason: a source checkout keeps its captures with the working tree, an
    installed package must not write into site-packages.
    """
    env = os.environ.get("EXCHANGE_DISCOVERY_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if is_source_checkout():
        return Path(__file__).resolve().parent.parent / ".discovery-sessions"
    return Path.home() / "owa-mcp" / "discovery-sessions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_login_host(host: str) -> bool:
    return any(hint in host for hint in _LOGIN_HOST_HINTS)


# Temp-profile housekeeping. A Chromium profile is tens of megabytes, and a
# discovery session creates a brand-new one every time, so failing to remove
# them quietly fills the user's temp directory.
_PROFILE_PREFIX = "owa-discovery-"
_STALE_PROFILE_AGE_SECONDS = 6 * 3600


def _remove_profile(path: Path, attempts: int = 6) -> bool:
    """Delete a temp profile directory, retrying while Chromium lets go of it.

    Two things make a single `rmtree` unreliable here, both observed on
    Windows: the browser process releases its lock on the profile slightly
    *after* `context.close()` returns (the same race
    `BrowserSession._async_relaunch` sleeps through), and antivirus/indexer
    handles can hold individual files for a second or two longer. Retrying
    turns "leaks a profile per session" into "leaks one only if something is
    genuinely stuck", and the return value is recorded in the manifest so a
    leak is visible instead of silent.
    """
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(1 + attempt)
    return not path.exists()


def sweep_stale_profiles(max_age_seconds: int = _STALE_PROFILE_AGE_SECONDS) -> int:
    """Remove abandoned discovery profiles left in the temp directory.

    The retry loop above covers the ordinary case, but not a server killed
    between a session ending and cleanup finishing - the cleanup runs on a
    daemon thread, which process exit terminates outright. Since every
    profile this module creates is named `owa-discovery-*` and lives in the
    system temp directory, a sweep at the start of the next session is a
    reliable second chance. Returns how many were removed.
    """
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        candidates = list(root.glob(f"{_PROFILE_PREFIX}*"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if not candidate.is_dir() or candidate.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if _remove_profile(candidate, attempts=1):
            removed += 1
    return removed


class DiscoveryRecorder:
    """One recording session: its own Chromium, its own temp profile, its own files."""

    def __init__(
        self,
        owa_url: str,
        *,
        scope: str,
        notes: str = "",
        start_url: str | None = None,
        capture_response_bodies: bool = False,
        keep_profile: bool = False,
        sessions_dir: Path | None = None,
    ):
        self.owa_url = owa_url.rstrip("/")
        self.owa_host = urlparse(self.owa_url).netloc.lower()
        self.scope = scope
        self.notes = notes
        self.start_url = start_url or f"{self.owa_url}/owa/"
        self.capture_response_bodies = capture_response_bodies
        self.keep_profile = keep_profile

        self.session_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        root = Path(sessions_dir) if sessions_dir else default_discovery_dir()
        self.session_dir = root / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)

        # Second chance at cleaning up after a server that was killed before a
        # previous session's teardown finished - see sweep_stale_profiles.
        self.stale_profiles_removed = sweep_stale_profiles()

        self.profile_dir = Path(tempfile.mkdtemp(prefix=f"{_PROFILE_PREFIX}{self.session_id}-"))
        self.profile_removed: bool | None = None

        self.state = "created"
        self.started_at = _now()
        self.ended_at: str | None = None
        self.counters = {
            "api_calls": 0,
            "ui_actions": 0,
            "navigations": 0,
            "noise_filtered": 0,
            "login_traffic_dropped": 0,
            "request_failures": 0,
        }

        self._lock = threading.Lock()
        self._last_ui_label = ""
        self._network_file = None
        self._actions_file = None
        self._finalized = False

        self._playwright = None
        self._context = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"owa-discovery-{self.session_id}"
        )
        self._thread.start()

    # ------------------------------------------------------------------
    # Loop plumbing (same shape as BrowserSession's, deliberately)
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def network_path(self) -> Path:
        return self.session_dir / "network.jsonl"

    @property
    def actions_path(self) -> Path:
        return self.session_dir / "actions.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.session_dir / "manifest.json"

    @property
    def report_json_path(self) -> Path:
        return self.session_dir / "report.json"

    @property
    def report_md_path(self) -> Path:
        return self.session_dir / "report.md"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, timeout: float = 180) -> dict:
        """Launch the visible recording window. Returns as soon as it's up.

        Never waits for the user: an MCP call can't be held open for a
        sign-in plus a browsing session, so this is start-and-poll exactly
        like the `login` tool's two-call flow.
        """
        self._network_file = self.network_path.open("a", encoding="utf-8")
        self._actions_file = self.actions_path.open("a", encoding="utf-8")
        self._write_manifest()
        self._run(self._async_start(), timeout=timeout)
        self.state = "recording"
        self._write_manifest()
        return self.status()

    async def _async_start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=False,  # the entire point is that a human drives it
            viewport={"width": 1440, "height": 960},
        )

        # Bindings and init scripts must be installed before the document we
        # want them in is created, hence before the navigation below.
        await self._context.expose_binding(_JS_BINDING, self._on_ui_event)
        await self._context.add_init_script(_INIT_SCRIPT)

        self._context.on("response", self._on_response)
        self._context.on("requestfailed", self._on_request_failed)
        self._context.on("page", self._on_page)
        self._context.on("close", self._on_context_closed)

        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()
        self._on_page(page)
        try:
            await page.goto(self.start_url, wait_until="commit", timeout=60000)
        except Exception:
            pass  # the user can navigate themselves; a failed first hop isn't fatal

    def _on_page(self, page) -> None:
        page.on("framenavigated", self._on_frame_navigated)

    def stop(self, timeout: float = 60) -> dict:
        """Close the window and finalize, for ending a session without closing it by hand.

        Returns immediately when the session has already ended - the common
        case being that the user closed the window a moment before this was
        called. That short-circuit is load-bearing: `_finalize` stops the
        browser loop on its way out, so scheduling a coroutine onto it
        afterwards would never complete and this call would sit on its full
        timeout before failing.
        """
        with self._lock:
            already_done = self._finalized
        if already_done:
            return self.status()

        try:
            self._run(self._async_close_context(), timeout=timeout)
        except Exception:
            pass
        self._finalize("stopped")
        return self.status()

    async def _async_close_context(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass

    def _on_context_closed(self) -> None:
        """The user closed the last window - the normal end of a session."""
        self._finalize("finished")

    def _finalize(self, state: str) -> None:
        """Flush files, tear down Playwright and the loop, drop the temp profile.

        Idempotent: reached both from the context-close event (user closed the
        window) and from `stop()`, and those can race.
        """
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            self.state = state
            self.ended_at = _now()

        for handle in (self._network_file, self._actions_file):
            try:
                if handle:
                    handle.flush()
                    handle.close()
            except Exception:
                pass
        self._network_file = self._actions_file = None

        self._write_manifest()

        # Stopping Playwright has to happen on its own loop, and _finalize can
        # be called *from* that loop (the close handler). Schedule it instead
        # of blocking on it, then stop the loop once it has run.
        def _teardown() -> None:
            async def _inner() -> None:
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                self._playwright = None

            asyncio.run_coroutine_threadsafe(_inner(), self._loop)
            time.sleep(1)
            self._loop.call_soon_threadsafe(self._loop.stop)
            if not self.keep_profile:
                self.profile_removed = _remove_profile(self.profile_dir)
                self._write_manifest()

        threading.Thread(
            target=_teardown, daemon=True, name=f"owa-discovery-teardown-{self.session_id}"
        ).start()

    # ------------------------------------------------------------------
    # Capture: network
    # ------------------------------------------------------------------

    def _on_request_failed(self, request) -> None:
        with self._lock:
            self.counters["request_failures"] += 1

    def _on_response(self, response) -> None:
        """Schedule the async body read - event handlers themselves stay sync.

        Recording on the *response* rather than the request is what guarantees
        request/response correlation: `response.request` is the very request
        that produced it, so there's no matching heuristic to get wrong. The
        cost is that a request which never completes isn't recorded at all,
        which `request_failures` counts.
        """
        asyncio.ensure_future(self._record_response(response), loop=self._loop)

    async def _record_response(self, response) -> None:
        try:
            request = response.request
            url = request.url
            parsed = urlparse(url)
            host = parsed.netloc.lower()

            if _is_login_host(host):
                with self._lock:
                    self.counters["login_traffic_dropped"] += 1
                return

            interesting_host = host == self.owa_host or any(h in host for h in _SERVICE_HOST_HINTS)
            path_lower = parsed.path.lower()
            is_noise = any(hint in path_lower or hint in host for hint in _NOISE_PATH_HINTS)

            if (
                request.resource_type not in _API_RESOURCE_TYPES
                or not interesting_host
                or is_noise
            ):
                with self._lock:
                    self.counters["noise_filtered"] += 1
                return

            headers = {k: v for k, v in request.headers.items() if k.lower() in _HEADER_ALLOWLIST}
            body, header_payload = self._read_request_payload(request)

            record = {
                "type": "request",
                "at": _now(),
                "method": request.method,
                "url": url,
                "path": parsed.path,
                "action": self._extract_action(parsed.query, request.headers),
                "resource_type": request.resource_type,
                "status": response.status,
                "headers": headers,
                "header_payload": header_payload,
                "body": body,
                "ui_hint": self._last_ui_label,
            }
            record.update(await self._read_response(response))
            self._write(self._network_file, record)
            with self._lock:
                self.counters["api_calls"] += 1
        except Exception:
            # A response can be discarded by the browser mid-read (SPA
            # navigation, aborted fetch). Losing one record must never take
            # down the recorder.
            pass

    @staticmethod
    def _extract_action(query: str, headers: dict) -> str:
        """The EWS action name, from `?action=` or the `Action` request header."""
        values = parse_qs(query).get("action") or parse_qs(query).get("Action")
        if values:
            return values[0]
        return headers.get("action", "") or headers.get("Action", "")

    def _read_request_payload(self, request) -> tuple[object, bool]:
        """Parse the request body, including the `X-OWA-UrlPostData` variant.

        Classic OWA sends several actions (`CreateFolder`, `UpdateFolder`,
        `GetMasterCategoryList`, ...) with an empty body and the JSON
        URL-encoded into that header instead - see
        `OWAClient.request_header_payload`. A recorder that only read
        `post_data` would capture those as parameterless calls, which is
        exactly the class of mistake that produces a wrong "no new
        parameters" verdict. Returns (parsed_or_raw, came_from_header).
        """
        header_data = request.headers.get("x-owa-urlpostdata") or request.headers.get("X-OWA-UrlPostData")
        if header_data:
            return self._parse_json(unquote(header_data)), True
        post_data = request.post_data
        if not post_data:
            return None, False
        return self._parse_json(post_data), False

    @staticmethod
    def _parse_json(text: str):
        """Parse JSON, falling back to a truncated raw string."""
        try:
            return json.loads(text)
        except Exception:
            return text[:4000]

    async def _read_response(self, response) -> dict:
        """Shape (and optionally body) of a JSON response.

        Only the *shape* by default: key names and value types with every
        value discarded. It's what an implementation needs to map a response,
        and it keeps subjects, addresses and attachment names out of the
        capture entirely.
        """
        content_type = (response.headers or {}).get("content-type", "")
        if "json" not in content_type.lower():
            return {}
        try:
            text = await response.text()
        except Exception:
            return {}
        if not text or len(text) > _MAX_BODY_BYTES:
            return {"response_truncated": True}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        out = {"response_shape": json_shape(parsed)}
        if self.capture_response_bodies:
            out["response_body"] = parsed
        return out

    # ------------------------------------------------------------------
    # Capture: UI
    # ------------------------------------------------------------------

    def _on_ui_event(self, source, payload) -> None:
        """Binding target for `_INIT_SCRIPT`. Never receives input values."""
        try:
            url = str(payload.get("url", ""))
            if _is_login_host(urlparse(url).netloc.lower()):
                return  # a click on the sign-in page tells us nothing worth keeping
            label = payload.get("label") or payload.get("text") or payload.get("testid") or payload.get("tag") or ""
            record = {
                "type": "ui",
                "at": _now(),
                "kind": payload.get("kind", "event"),
                "label": str(label)[:120],
                "tag": payload.get("tag", ""),
                "role": payload.get("role", ""),
                "testid": payload.get("testid", ""),
                "url": url,
            }
            self._write(self._actions_file, record)
            with self._lock:
                self._last_ui_label = record["label"]
                self.counters["ui_actions"] += 1
        except Exception:
            pass

    def _on_frame_navigated(self, frame) -> None:
        try:
            if frame.parent_frame is not None:
                return  # main frame only - iframes are chrome, not user intent
            url = frame.url
            if _is_login_host(urlparse(url).netloc.lower()):
                return
            self._write(self._actions_file, {"type": "ui", "at": _now(), "kind": "navigate",
                                             "label": url, "url": url})
            with self._lock:
                self.counters["navigations"] += 1
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _write(self, handle, record: dict) -> None:
        if handle is None:
            return
        try:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()  # a capture is only useful if it survives a crash
        except Exception:
            pass

    def _write_manifest(self) -> None:
        try:
            self.manifest_path.write_text(
                json.dumps(self.manifest(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def manifest(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "scope": self.scope,
            "notes": self.notes,
            "owa_url": self.owa_url,
            "start_url": self.start_url,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "profile_dir": str(self.profile_dir),
            "profile_kept": self.keep_profile,
            "profile_removed": self.profile_removed,
            "stale_profiles_removed": self.stale_profiles_removed,
            "capture_response_bodies": self.capture_response_bodies,
            "session_dir": str(self.session_dir),
            "counters": dict(self.counters),
        }

    def status(self) -> dict:
        info = self.manifest()
        info["recording"] = self.state == "recording"
        return info


# ----------------------------------------------------------------------
# Process-wide registry
# ----------------------------------------------------------------------
#
# Module-level rather than per-AppContext, for the same reason the login
# tool's pending task is: under --transport http a fresh AppContext is built
# per client session, and a discovery session that starts on one call and is
# polled on the next has to survive that.

_registry_lock = threading.Lock()
_recorders: dict[str, DiscoveryRecorder] = {}
_latest_id: str | None = None


def register(recorder: DiscoveryRecorder) -> None:
    global _latest_id
    with _registry_lock:
        _recorders[recorder.session_id] = recorder
        _latest_id = recorder.session_id


def get_recorder(session_id: str | None) -> DiscoveryRecorder | None:
    """Look up a live recorder; `None` resolves to the most recent one."""
    with _registry_lock:
        if session_id:
            return _recorders.get(session_id)
        return _recorders.get(_latest_id) if _latest_id else None


def active_recorders() -> list[DiscoveryRecorder]:
    with _registry_lock:
        return [r for r in _recorders.values() if r.state == "recording"]


@atexit.register
def _stop_active_recorders() -> None:
    for recorder in active_recorders():
        try:
            recorder.stop(timeout=15)
        except Exception:
            pass


# ----------------------------------------------------------------------
# Reading captures back from disk
# ----------------------------------------------------------------------
#
# Classification deliberately works off the files, not the live recorder, so a
# session recorded before a server restart can still be analysed - and so the
# analysis path is testable against fixtures with no browser anywhere.

_SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL capture file, skipping any line that didn't survive a crash."""
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records


def session_dir_for(session_id: str, sessions_dir: Path | None = None) -> Path:
    root = Path(sessions_dir) if sessions_dir else default_discovery_dir()
    return root / session_id


def load_session(session_id: str, sessions_dir: Path | None = None) -> dict:
    """Load one capture from disk: manifest plus both record streams."""
    directory = session_dir_for(session_id, sessions_dir)
    manifest_path = directory / "manifest.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    return {
        "session_id": session_id,
        "session_dir": str(directory),
        "exists": directory.exists(),
        "manifest": manifest,
        "network": read_jsonl(directory / "network.jsonl"),
        "ui_actions": read_jsonl(directory / "actions.jsonl"),
    }


def list_sessions(sessions_dir: Path | None = None) -> list[dict]:
    """Every capture directory on disk, newest first, live state merged in."""
    root = Path(sessions_dir) if sessions_dir else default_discovery_dir()
    if not root.exists():
        return []

    sessions: list[dict] = []
    for directory in sorted(root.iterdir(), reverse=True):
        if not directory.is_dir() or not _SESSION_ID_RE.match(directory.name):
            continue
        live = get_recorder(directory.name)
        if live is not None:
            info = live.status()
        else:
            info = {"session_id": directory.name, "state": "unknown", "session_dir": str(directory)}
            manifest_path = directory / "manifest.json"
            if manifest_path.exists():
                try:
                    info.update(json.loads(manifest_path.read_text(encoding="utf-8")))
                except Exception:
                    pass
        info["has_report"] = (directory / "report.json").exists()
        sessions.append(info)
    return sessions
