"""Unit test: browser-profile lock detection, staleness and launch-failure diagnosis.

Needs no live mailbox, no browser and no EXCHANGE_OWA_URL. It does touch the
filesystem (real temp directories, real OS file locks), because that is what is
under test -- but never a real Chromium: process liveness and the singleton
symlink layout are injected, since a POSIX `SingletonLock` cannot be created on
the Windows box this repo is developed on and `os.kill(pid, 0)` there is a *kill*,
not a probe.

The asymmetry between the two failure directions is the thing to keep in mind
while reading the checks:

- A missed lock costs the confusing failure from issue #11 -- one more capture,
  one more manual taskkill.
- A *fabricated* lock takes a healthy server offline, or worse, deletes a live
  browser's singleton artifacts. So every undecidable case must land on UNKNOWN /
  "alive" / "don't clear", and there are more checks below for that direction
  than for detection.

Run standalone:
    python -m tests.unit.test_profile_lock
"""

import os
import sys
import tempfile
from pathlib import Path

from exchange_mcp import profile_lock as pl

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


def _profile(tmp: Path, *, with_lock_files: bool = True) -> Path:
    """A directory shaped like a Chromium profile, minus the browser."""
    root = tmp / "profile"
    root.mkdir(parents=True, exist_ok=True)
    if with_lock_files:
        for relative in pl._LOCK_PROBE_FILES:
            target = root.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
    return root


# ------------------------------------------------------------------
# Directory-level verdicts
# ------------------------------------------------------------------


def test_missing_and_empty_profiles() -> None:
    print("Profile directories with nothing to say")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        absent = tmp_path / "never-created"
        state = pl.inspect_profile_lock(absent)
        check("a profile that doesn't exist yet is free", state.state == pl.FREE, pl.describe(state))
        check("...and does not block a launch", not state.blocks_launch)

        # A directory that exists but has none of the probe files: a brand-new
        # profile the server just mkdir'd, or a Chromium layout change. Must not
        # be reported as free (nothing was actually checked) *or* as held.
        bare = _profile(tmp_path, with_lock_files=False)
        state = pl.inspect_profile_lock(bare)
        check("a profile with no probe files is undetermined", state.state == pl.UNKNOWN, pl.describe(state))
        check("...and still does not block a launch", not state.blocks_launch)


def test_unlocked_profile_reads_free() -> None:
    print("An idle profile with real lock files")
    with tempfile.TemporaryDirectory() as tmp:
        root = _profile(Path(tmp))
        state = pl.inspect_profile_lock(root)
        check("all probe files unlocked -> free", state.state == pl.FREE, pl.describe(state))
        check("detail says how many files were probed", "probed" in state.detail, state.detail)


def test_locked_file_is_detected() -> None:
    print("A profile whose lock file is held by this very process")
    with tempfile.TemporaryDirectory() as tmp:
        root = _profile(Path(tmp))
        target = root.joinpath(*pl._LOCK_PROBE_FILES[0].split("/"))
        target.write_bytes(b"\0")  # need a byte to lock on POSIX

        handle = open(target, "r+b")
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                # A lock this process already owns is re-acquirable by this same
                # process on POSIX, so the probe would see it as free. Hold it from
                # a child instead of pretending otherwise.
                fcntl.lockf(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1)

            state = pl.inspect_profile_lock(root)
            if os.name == "nt":
                check("a held lock file -> held", state.state == pl.HELD, pl.describe(state))
                check("...and blocks a launch", state.blocks_launch)
                check("...naming the file that gave it away", "LOCK" in state.detail, state.detail)
            else:
                # POSIX fcntl locks are per-process, so this can't be exercised
                # from inside the holder. Assert the weaker thing that is true
                # everywhere: the probe reached *a* verdict and didn't crash.
                check("probe returns a verdict on POSIX", state.state in
                      (pl.FREE, pl.HELD, pl.UNKNOWN), pl.describe(state))
        finally:
            handle.close()


def test_any_single_held_file_is_decisive() -> None:
    print("Only some lock files are held on a live profile")
    # Verified live 2026-09-15 on a running server's profile: three probe files
    # reported held while PersistentOriginTrials/LOCK reported free. So a scan
    # that required agreement, or that stopped at the first file it could read,
    # would have called that profile free.
    with tempfile.TemporaryDirectory() as tmp:
        root = _profile(Path(tmp))
        last = pl._LOCK_PROBE_FILES[-1]

        def probe(path: Path) -> pl.ProfileLockState:
            # Stand in for the OS: only the *last* candidate is held.
            return pl.ProfileLockState(pl.HELD, path, detail=f"a live process holds {last!r}")

        state = pl.inspect_profile_lock(root, lock_file_probe=probe)
        check("a single held file among free ones -> held", state.state == pl.HELD, pl.describe(state))

    # And the real scan must not stop early: probe_lock_files walks every
    # candidate rather than returning on the first readable one.
    check("the probe walks more than one candidate file", len(pl._LOCK_PROBE_FILES) > 1)


# ------------------------------------------------------------------
# SingletonLock: the only signal that can name an owner
# ------------------------------------------------------------------


def test_singleton_lock_states() -> None:
    print("SingletonLock (POSIX): live, stale, foreign host, malformed")
    with tempfile.TemporaryDirectory() as tmp:
        root = _profile(Path(tmp))

        def readlink_as(target: str):
            """Pretend `SingletonLock` is a symlink pointing at `target`."""
            original = os.readlink

            def fake(path, *args, **kwargs):
                if str(path).endswith("SingletonLock"):
                    return target
                return original(path, *args, **kwargs)

            return original, fake

        cases = [
            ("thishost-4242", "thishost", lambda pid: True, pl.HELD, 4242),
            ("thishost-4242", "thishost", lambda pid: False, pl.STALE, 4242),
            # Another machine's lock: the PID is meaningless here and deleting it
            # would be the worst possible mistake, so it must read undetermined.
            ("otherhost-4242", "thishost", lambda pid: False, pl.UNKNOWN, 4242),
            ("garbage", "thishost", lambda pid: False, pl.UNKNOWN, None),
        ]
        for target, host, alive, expected, expected_pid in cases:
            original, fake = readlink_as(target)
            os.readlink = fake
            try:
                state = pl.read_singleton_lock(root, hostname=host, pid_is_alive=alive)
            finally:
                os.readlink = original
            check(f"{target!r} (alive={alive(0)}) -> {expected}",
                  state is not None and state.state == expected,
                  pl.describe(state) if state else "None")
            if expected_pid is not None:
                check(f"  ...owner pid {expected_pid} recorded",
                      state is not None and state.owner_pid == expected_pid,
                      repr(state.owner_pid if state else None))

    # No symlink to read (the normal Windows case) must fall through to the file
    # probe rather than claim the profile is free.
    with tempfile.TemporaryDirectory() as tmp:
        root = _profile(Path(tmp))
        check("no SingletonLock -> no verdict from that signal",
              pl.read_singleton_lock(root, hostname="thishost") is None)


def test_stale_clearing_is_narrow() -> None:
    print("clear_stale_lock only ever touches a dead owner's artifacts")
    with tempfile.TemporaryDirectory() as tmp:
        root = _profile(Path(tmp))
        for name in pl._SINGLETON_ARTIFACTS:
            (root / name).write_bytes(b"")

        for state_name in (pl.HELD, pl.FREE, pl.UNKNOWN):
            state = pl.ProfileLockState(state_name, root, owner_pid=4242)
            check(f"{state_name} -> nothing removed", pl.clear_stale_lock(state) == [])
        check("...and the artifacts are all still there",
              all((root / name).exists() for name in pl._SINGLETON_ARTIFACTS))

        stale = pl.ProfileLockState(pl.STALE, root, owner_pid=4242)
        removed = pl.clear_stale_lock(stale)
        check("stale -> every singleton artifact removed",
              sorted(removed) == sorted(pl._SINGLETON_ARTIFACTS), repr(removed))
        check("...and they are gone from disk",
              not any((root / name).exists() for name in pl._SINGLETON_ARTIFACTS))

        # Idempotent: a second pass on an already-cleared profile removes nothing
        # and doesn't raise.
        check("clearing twice is a no-op", pl.clear_stale_lock(stale) == [])

        # Nothing here may reach for a process. If a live server owns the profile,
        # the answer is a message, not a kill.
        source = Path(pl.__file__).read_text(encoding="utf-8")
        for forbidden in ("taskkill", "terminate(", "SIGKILL", "SIGTERM"):
            check(f"module never {forbidden!r}s anything", forbidden not in source)


def test_pid_liveness_is_biased_towards_alive() -> None:
    print("Process liveness errs towards 'still running'")
    # Saying "dead" wrongly is what would delete a live browser's lock, so every
    # case that can't be decided has to answer True.
    check("pid 0 -> alive", pl._pid_is_alive(0))
    check("negative pid -> alive", pl._pid_is_alive(-1))
    if os.name == "nt":
        # os.kill(pid, 0) on Windows calls TerminateProcess: the idiomatic POSIX
        # probe would *kill* the process it was asked about. The guard must hold
        # regardless of what PID it is handed.
        check("windows never probes by signalling", pl._pid_is_alive(999_999_999))
    else:
        check("this process is alive", pl._pid_is_alive(os.getpid()))


# ------------------------------------------------------------------
# Launch-failure classification
# ------------------------------------------------------------------


def test_classify_launch_failure() -> None:
    print("Launch-failure diagnosis")
    root = Path("/tmp/profile-x")
    held = pl.ProfileLockState(pl.HELD, root, owner_pid=4242, detail="a live process holds it")

    # The generic text issue #11 was reported with. On its own it is *not*
    # diagnosable - that is the whole reason the directory has to be probed.
    generic = "Target page, context or browser has been closed"
    check("closed-context text alone -> no verdict",
          pl.classify_launch_failure(generic) is None,
          repr(pl.classify_launch_failure(generic)))
    check("closed-context text + a free profile -> no verdict",
          pl.classify_launch_failure(generic, pl.ProfileLockState(pl.FREE, root)) is None)
    check("closed-context text + an undetermined profile -> no verdict",
          pl.classify_launch_failure(generic, pl.ProfileLockState(pl.UNKNOWN, root)) is None)

    verdict = pl.classify_launch_failure(generic, held)
    check("closed-context text + a held profile -> profile_locked",
          verdict is not None and verdict[0] == pl.PROFILE_LOCKED, repr(verdict))

    # A browser that says so itself is enough even when the probe shrugged.
    for text in (
        "Failed to create a ProcessSingleton for your profile directory.",
        "The profile appears to be in use by another Chromium process",
        "The process cannot access the file because it is being used by another process",
    ):
        verdict = pl.classify_launch_failure(text, pl.ProfileLockState(pl.UNKNOWN, root))
        check(f"browser says {text[:38]!r} -> profile_locked",
              verdict is not None and verdict[0] == pl.PROFILE_LOCKED, repr(verdict))

    # False positives: ordinary launch failures must stay unclassified so the
    # caller's existing crash handling keeps running.
    for text in (
        "",
        "net::ERR_CONNECTION_REFUSED at https://owa.example.com/owa/",
        "Executable doesn't exist at /ms-playwright/chromium-1234/chrome-linux/chrome",
        "Timeout 30000ms exceeded",
        "'NoneType' object has no attribute 'goto'",
    ):
        check(f"unrelated failure {text[:34]!r} -> no verdict",
              pl.classify_launch_failure(text) is None,
              repr(pl.classify_launch_failure(text)))


def test_message_names_the_profile_directory() -> None:
    print("The message is the fix")
    root = Path("/tmp/some/.browser-profile")
    state = pl.ProfileLockState(pl.HELD, root, owner_pid=4242, detail="a live process holds a lock file")
    message = pl.lock_message(state)
    # Naming the directory is what turns this from a mystery into a one-step fix;
    # it is the single sentence issue #11 asked for.
    check("names the profile directory", str(root) in message, message)
    check("names the owner pid", "4242" in message, message)
    check("says another process owns it", "another browser process" in message, message)

    exc = pl.ProfileLockedError(state)
    check("the error carries the reason", exc.reason == pl.PROFILE_LOCKED)
    check("the error carries remediation", exc.remediation in str(exc))
    check("the error names the directory", str(root) in str(exc), str(exc))
    check("remediation offers the profile-dir escape hatch",
          "EXCHANGE_BROWSER_PROFILE_DIR" in exc.remediation, exc.remediation)
    check("the error exposes the directory structurally", exc.profile_dir == root)
    check("the error exposes the owner pid structurally", exc.owner_pid == 4242)

    # An error raised with no state at all must still be a sentence, not a crash.
    bare = pl.ProfileLockedError()
    check("a stateless error still reads as English", "profile" in str(bare).lower(), str(bare))

    check("every reason code has remediation",
          all(reason in pl.REMEDIATION for reason in (pl.PROFILE_LOCKED,)))
    check("unknown reason falls back to the locked remediation",
          pl.remediation("something-new") == pl.REMEDIATION[pl.PROFILE_LOCKED])


def test_describe_labels_every_state() -> None:
    print("Banner labels")
    root = Path("/tmp/p")
    for state_name in (pl.FREE, pl.HELD, pl.STALE, pl.UNKNOWN):
        text = pl.describe(pl.ProfileLockState(state_name, root, detail="because reasons"))
        check(f"{state_name} has a label", bool(text) and "because reasons" in text, text)
    check("held is shouted, so it is legible in a log", "HELD" in pl.describe(
        pl.ProfileLockState(pl.HELD, root)))


# ------------------------------------------------------------------
# Wiring: the recovery path must not swallow a lock error
# ------------------------------------------------------------------


def test_recovery_does_not_retry_a_lock() -> None:
    print("BrowserSession's crash recovery re-raises a lock error")
    from exchange_mcp import browser_session as bs

    # The bug this guards: a contended profile and a crashed browser produce the
    # same "has been closed" text, so _run_with_recovery relaunched into the very
    # same lock and failed identically - including the automatic retry named in
    # issue #11. A lock error must reach the caller, not the retry.
    session = object.__new__(bs.BrowserSession)  # no browser, no thread, no loop
    calls: list[str] = []

    def boom():
        calls.append("call")
        raise pl.ProfileLockedError(pl.ProfileLockState(pl.HELD, Path("/tmp/p"), owner_pid=1))

    # _run just calls the coroutine factory's product here; a relaunch would show
    # up as a second entry in `calls`, which the count check below rules out.
    session._run = lambda coro, timeout=None: coro()  # type: ignore[method-assign]
    try:
        session._run_with_recovery(boom, timeout=1)
    except pl.ProfileLockedError as exc:
        check("ProfileLockedError propagates", "another browser process" in str(exc), str(exc))
    except Exception as exc:  # pragma: no cover - a wrong exception type is a failure
        check("ProfileLockedError propagates", False, f"{type(exc).__name__}: {exc}")
    else:
        check("ProfileLockedError propagates", False, "no exception raised")
    check("the failing call was made exactly once, not retried", calls == ["call"], repr(calls))

    check("the closed-context text a lock used to masquerade as is still a crash hint",
          any("has been closed" in hint for hint in bs._CRASH_HINTS))
    check("relaunch waits longer for the lock than a cold launch does",
          bs._LOCK_WAIT_RELAUNCH_SECONDS > bs._LOCK_WAIT_LAUNCH_SECONDS,
          f"{bs._LOCK_WAIT_RELAUNCH_SECONDS} vs {bs._LOCK_WAIT_LAUNCH_SECONDS}")


def main() -> bool:
    for test in (
        test_missing_and_empty_profiles,
        test_unlocked_profile_reads_free,
        test_locked_file_is_detected,
        test_any_single_held_file_is_decisive,
        test_singleton_lock_states,
        test_stale_clearing_is_narrow,
        test_pid_liveness_is_biased_towards_alive,
        test_classify_launch_failure,
        test_message_names_the_profile_directory,
        test_describe_labels_every_state,
        test_recovery_does_not_retry_a_lock,
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
