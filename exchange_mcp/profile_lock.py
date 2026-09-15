"""Diagnosis of a Chromium profile directory that is already in use.

`BrowserSession` keeps one persistent Chromium on one on-disk profile, and that
profile is meant to have exactly one owner: two browsers writing the same
`user-data-dir` is what Chromium's own ProcessSingleton exists to prevent. When
that invariant breaks, the symptom the operator sees is useless -- issue #11
recorded an orphaned Chromium tree still holding `.browser-profile` while every
subsequent launch, *including the one-retry crash recovery*, reported only
"Target page, context or browser has been closed." Nothing in that sentence says
"another process owns your profile directory", which is the one fact that turns
it into a one-step fix.

This module answers that question, and it differs from `auth_errors.py` in one
structural way worth stating up front: **the error text alone cannot decide it.**
A crashed browser and a contended profile produce the *same* closed-context
message, so classification takes the launch error *plus* a live look at the
profile directory. The signal tables are still the correctable-in-one-place kind,
they just aren't the only input.

Two verified facts shape the probe, both established empirically on 2026-09-15:

- **Playwright's Chromium does not fail fast on a contended profile.** A second
  `launch_persistent_context` against a profile a live browser was holding
  launched and was usable. So a lock cannot be detected by waiting for the launch
  to error -- it has to be checked *before* launching, which is why
  `inspect_profile_lock` exists at all rather than a pure error-text table.
- **`SingletonLock` does not exist on Windows.** Chromium's ProcessSingleton uses
  a named mutex there, so the documented `<host>-<pid>` symlink -- the only
  artifact that can name an owner and therefore the only one that can be judged
  *stale* -- is a POSIX-only signal. What holds on both is that Chromium takes OS
  file locks on its LevelDB `LOCK` files: probing those correctly reported a live
  server's profile as held and an idle profile as free. Note that only *some* of
  them are held (`PersistentOriginTrials/LOCK` read free while three others read
  held on the same live profile), hence a candidate list where any single held
  file is decisive and "free" requires every probed file to agree.

Deliberate design points:

- **Only a positively-detected live lock blocks a launch.** Every path that
  cannot reach a verdict returns UNKNOWN, and UNKNOWN never blocks. A false
  "someone else owns this" would take the server down for no reason, which is
  strictly worse than the confusing message this module replaces.
- **A stale lock is cleared; a live one is never touched.** Clearing means
  unlinking Chromium's own singleton artifacts whose named owner is gone -- never
  killing a process. If another server is genuinely running, failing fast with a
  message naming the directory is the correct outcome, not a fight over the
  profile.
- Pure logic, no Playwright import, so it stays unit-testable
  (tests/unit/test_profile_lock.py). The filesystem and process probes are
  injectable for the same reason.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# ------------------------------------------------------------------
# Lock states
# ------------------------------------------------------------------

# Nothing owns the profile: safe to launch.
FREE = "free"

# A live process owns it. This is the only state that blocks a launch.
HELD = "held"

# Singleton artifacts are present but their named owner is gone. Recoverable
# here, by removing the artifacts (never by killing anything).
STALE = "stale"

# No verdict could be reached -- an unreadable artifact, a profile owned by
# another host, or a platform/Chromium layout with none of the probe files.
# Treated as "go ahead and launch": see the module docstring.
UNKNOWN = "unknown"


# ------------------------------------------------------------------
# Reason codes
# ------------------------------------------------------------------

# The profile directory is owned by another browser process. A retry cannot help;
# a human has to stop the other server (or point this one at another directory).
PROFILE_LOCKED = "profile_locked"

REMEDIATION: dict[str, str] = {
    PROFILE_LOCKED: "Another browser process owns this profile directory, and two servers cannot "
                    "share one. Stop the other exchange-mcp server (or the orphaned Chromium tree "
                    "left behind by one) and let this server retry, or start this one with "
                    "EXCHANGE_BROWSER_PROFILE_DIR pointing at a directory of its own.",
}

# Chromium's singleton bookkeeping, all POSIX-only. `SingletonLock` is a symlink
# whose target is `<hostname>-<pid>`, which is what makes an owner identifiable
# and therefore what makes "stale" a decidable state; the other two are the
# rendezvous socket and its cookie, removed alongside it so a fresh launch isn't
# left arguing with half a lock.
_SINGLETON_ARTIFACTS = ("SingletonLock", "SingletonSocket", "SingletonCookie")

# LevelDB lock files Chromium holds an OS lock on for as long as a browser is
# live on the profile. The cross-platform probe, and the only one available on
# Windows. Any one of them reporting held is decisive; "free" needs all the
# present ones to agree, because a live profile was observed with some of these
# free and others held.
_LOCK_PROBE_FILES = (
    "Default/Local Storage/leveldb/LOCK",
    "Default/shared_proto_db/LOCK",
    "Default/shared_proto_db/metadata/LOCK",
    "Default/PersistentOriginTrials/LOCK",
)

# Text a browser or OS emits when a launch loses a fight over the profile. Used
# only as a *fallback* for when the directory probe reached no verdict: a browser
# that says this outright is better evidence than a probe that shrugged. Matched
# case-insensitively as substrings, so each has to be specific enough that no
# healthy launch produces it.
_LOCK_ERROR_HINTS: tuple[str, ...] = (
    # Chromium's own abort message when ProcessSingleton can't be created.
    "processsingleton",
    "profile appears to be in use",
    "profile directory is already in use",
    "user data directory is already in use",
    "singletonlock",
    # Windows ERROR_SHARING_VIOLATION (32), which is how a contended profile file
    # surfaces there -- Windows refuses the open outright rather than reporting a
    # lock, so the text is the only signal in that case.
    "being used by another process",
)


@dataclass(frozen=True)
class ProfileLockState:
    """What we could determine about who owns a profile directory.

    `detail` is written for a log line an operator reads, not for parsing, and
    always says *how* the verdict was reached -- a lock diagnosis that can't be
    audited is how the original confusing message survived so long.
    """

    state: str
    profile_dir: Path
    owner_pid: int | None = None
    owner_host: str | None = None
    detail: str = ""

    @property
    def blocks_launch(self) -> bool:
        return self.state == HELD


# ------------------------------------------------------------------
# Process liveness
# ------------------------------------------------------------------


def _pid_is_alive(pid: int) -> bool:
    """Best-effort "is this PID still running", biased towards saying yes.

    A wrong "no" is the expensive direction: it is what would let a *live*
    owner's singleton artifacts be deleted out from under it, so every case this
    can't decide reports True.

    Note the Windows branch is not laziness. `os.kill(pid, 0)` there does not
    send a signal at all -- it calls TerminateProcess, i.e. the idiomatic POSIX
    liveness probe *kills the process* on Windows. Nothing reaches this function
    on Windows today (the caller only gets here from a `SingletonLock` symlink,
    which Windows Chromium never writes), and that trap is exactly why the guard
    is here rather than left to the caller to remember.
    """
    if os.name == "nt":
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError means it exists and belongs to someone else.
        return True
    return True


# ------------------------------------------------------------------
# File-lock probe
# ------------------------------------------------------------------


def _probe_locked_file(path: Path) -> bool | None:
    """True if a live process holds `path`, False if not, None if undecidable.

    Takes the lock and immediately releases it. That briefly makes the file
    unlockable by anyone else, which is safe in practice: the window is
    microseconds, and the only contender would be a browser starting at that
    exact instant -- a race the caller is about to resolve anyway by launching.
    """
    try:
        handle = open(path, "r+b")
    except PermissionError:
        # Windows refuses the open outright when the holder shares nothing.
        return True
    except OSError as exc:
        if getattr(exc, "winerror", None) == 32:  # ERROR_SHARING_VIOLATION
            return True
        return None

    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return False

        import fcntl

        try:
            fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
        except OSError:
            return True
        fcntl.lockf(handle.fileno(), fcntl.LOCK_UN, 1)
        return False
    except Exception:
        return None
    finally:
        handle.close()


def probe_lock_files(profile_dir: Path) -> ProfileLockState:
    """Decide ownership from Chromium's LevelDB lock files.

    The cross-platform half of the probe, and the whole of it on Windows. A
    single held file settles it; FREE requires every file that exists to report
    free, because a live profile really does have some of them unlocked.
    """
    free_files: list[str] = []
    for relative in _LOCK_PROBE_FILES:
        candidate = profile_dir.joinpath(*relative.split("/"))
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        verdict = _probe_locked_file(candidate)
        if verdict is True:
            return ProfileLockState(
                HELD,
                profile_dir,
                detail=f"a live process holds the browser lock file {relative!r}",
            )
        if verdict is False:
            free_files.append(relative)

    if free_files:
        return ProfileLockState(
            FREE,
            profile_dir,
            detail=f"no process holds any of the {len(free_files)} browser lock file(s) probed",
        )
    return ProfileLockState(
        UNKNOWN,
        profile_dir,
        detail="none of the known Chromium lock files could be probed in this profile",
    )


# ------------------------------------------------------------------
# Singleton probe (POSIX)
# ------------------------------------------------------------------


def read_singleton_lock(
    profile_dir: Path,
    *,
    hostname: str,
    pid_is_alive=_pid_is_alive,
) -> ProfileLockState | None:
    """Read Chromium's `SingletonLock` symlink, or None if there isn't one to read.

    None means "no verdict from this signal, try the file probe" -- which is the
    normal case on Windows, where the symlink is never written at all.

    A lock naming *another host* is reported UNKNOWN rather than stale or held:
    the profile is on shared storage, the PID means nothing locally, and deleting
    another machine's lock is the one mistake this module must never make.
    """
    lock = profile_dir / "SingletonLock"
    try:
        target = os.readlink(lock)
    except (OSError, ValueError, NotImplementedError):
        return None

    owner_host, _, pid_text = str(target).rpartition("-")
    try:
        owner_pid = int(pid_text)
    except ValueError:
        return ProfileLockState(
            UNKNOWN,
            profile_dir,
            detail=f"SingletonLock points at {target!r}, which is not the documented <host>-<pid> form",
        )

    if owner_host and owner_host != hostname:
        return ProfileLockState(
            UNKNOWN,
            profile_dir,
            owner_pid=owner_pid,
            owner_host=owner_host,
            detail=f"SingletonLock is owned by host {owner_host!r}, not this one ({hostname!r})",
        )

    if pid_is_alive(owner_pid):
        return ProfileLockState(
            HELD,
            profile_dir,
            owner_pid=owner_pid,
            owner_host=owner_host or hostname,
            detail=f"SingletonLock names PID {owner_pid}, which is still running",
        )
    return ProfileLockState(
        STALE,
        profile_dir,
        owner_pid=owner_pid,
        owner_host=owner_host or hostname,
        detail=f"SingletonLock names PID {owner_pid}, which is no longer running",
    )


# ------------------------------------------------------------------
# Public entry points
# ------------------------------------------------------------------


def inspect_profile_lock(
    profile_dir: str | Path,
    *,
    hostname: str | None = None,
    pid_is_alive=_pid_is_alive,
    lock_file_probe=probe_lock_files,
) -> ProfileLockState:
    """Determine who, if anyone, owns `profile_dir`.

    Cheap enough to call before every launch and on every retry, which is the
    point: ownership changes when the other server exits, so a stale verdict
    cached from startup would be worse than no verdict.

    The signals are consulted in order of how much they can tell us:
    `SingletonLock` first, because it is the only one that names an owner and so
    the only one that can distinguish stale from live; the LevelDB file locks
    second, because they work everywhere but can only say held-or-not.
    """
    path = Path(profile_dir)
    if not path.exists():
        return ProfileLockState(FREE, path, detail="the profile directory does not exist yet")

    import socket

    singleton = read_singleton_lock(
        path,
        hostname=hostname if hostname is not None else socket.gethostname(),
        pid_is_alive=pid_is_alive,
    )
    if singleton is not None:
        return singleton

    return lock_file_probe(path)


def clear_stale_lock(state: ProfileLockState) -> list[str]:
    """Remove the singleton artifacts of a dead owner. Returns what was removed.

    Refuses to do anything unless the state is exactly STALE -- i.e. an owner was
    *identified* and found gone. HELD and UNKNOWN both mean something might still
    be using the profile, and clearing a live lock is how a "recovery" turns into
    two browsers corrupting one profile.

    Never touches a process. The orphaned-tree case from issue #11 still needs a
    human with a task manager; what changes is that they are now told so.
    """
    if state.state != STALE:
        return []

    removed: list[str] = []
    for name in _SINGLETON_ARTIFACTS:
        artifact = state.profile_dir / name
        try:
            # is_symlink() first: a dangling symlink -- exactly what a dead
            # owner leaves behind -- reports exists() False.
            if artifact.is_symlink() or artifact.exists():
                artifact.unlink()
                removed.append(name)
        except OSError:
            continue
    return removed


def describe(state: ProfileLockState) -> str:
    """One-line summary for the startup banner."""
    labels = {
        FREE: "free",
        HELD: "HELD BY ANOTHER PROCESS",
        STALE: "stale lock left by a dead process",
        UNKNOWN: "undetermined",
    }
    label = labels.get(state.state, state.state)
    return f"{label} ({state.detail})" if state.detail else label


def lock_message(state: ProfileLockState | None = None, browser_said: str = "") -> str:
    """The operator-facing explanation. Always names the profile directory.

    Naming the directory is the entire point of this module: the original failure
    was legible only to someone who already knew a profile lock existed.
    """
    where = f"'{state.profile_dir}'" if state is not None else "the browser profile directory"
    parts = [f"The browser profile directory {where} is already in use by another browser process."]
    if state is not None and state.owner_pid:
        parts.append(f"It reports owner PID {state.owner_pid}.")
    if state is not None and state.detail:
        parts.append(f"Detected because {state.detail}.")
    if browser_said:
        parts.append(f"The browser reported: {browser_said}")
    return " ".join(parts)


def classify_launch_failure(
    error_text: str = "", lock_state: ProfileLockState | None = None
) -> tuple[str, str] | None:
    """Map a failed browser launch to a (reason, message) pair, or None.

    None means "not a profile-lock problem" -- the caller keeps whatever generic
    handling it already had. That is the important half of the contract: a
    crashed browser and a contended profile produce the same closed-context text,
    so guessing PROFILE_LOCKED from a bare "has been closed" would relabel every
    ordinary crash as an operator error.
    """
    if lock_state is not None and lock_state.state == HELD:
        return PROFILE_LOCKED, lock_message(lock_state)

    lowered = (error_text or "").lower()
    if any(hint in lowered for hint in _LOCK_ERROR_HINTS):
        return PROFILE_LOCKED, lock_message(lock_state, browser_said=(error_text or "").strip())
    return None


def remediation(reason: str | None) -> str:
    return REMEDIATION.get(reason or "", REMEDIATION[PROFILE_LOCKED])


class ProfileLockedError(Exception):
    """Raised when the profile directory belongs to another browser process.

    Deliberately not folded into the crash-recovery path: `_CRASH_HINTS`-driven
    recovery relaunches and retries once, and issue #11 is precisely the case
    where that retry hits the same lock and fails identically. Retrying a live
    lock cannot succeed, so this exception carries its own remediation and
    propagates instead.
    """

    def __init__(self, state: ProfileLockState | None = None, message: str = "", reason: str = PROFILE_LOCKED):
        self.state = state
        self.reason = reason
        self.remediation = remediation(reason)
        self.profile_dir = state.profile_dir if state is not None else None
        self.owner_pid = state.owner_pid if state is not None else None
        super().__init__(f"{message or lock_message(state)} {self.remediation}".strip())
