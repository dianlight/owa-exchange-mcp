"""Run every pure-logic suite in tests/unit — the whole mailbox-free half of
the test story, in one command.

    python -m tests.unit

Each suite stays individually runnable (`python -m tests.unit.test_item_errors`)
because that is what you want while fixing one thing. This module exists for
the other case: CI, where the list of suites must not be able to go stale.

**Suites are discovered, not listed.** Issue #16 described "five suites" while
six were already on disk — the sixth (`test_copilot_answer_text`) landed days
after the list was written. A CI step spelling out `python -m tests.unit.<name>`
per suite fails the same way, except silently: the new suite passes locally,
never runs on a PR, and nobody finds out until it regresses. So this walks the
package directory for `test_*.py` instead, which is the same choice
`capability_inventory.py` makes about implemented capabilities (derive it from
the tree, don't maintain a second copy of the truth).

A suite is expected to expose `main() -> bool` (True = all checks passed), the
convention every existing suite already follows. One that doesn't is reported as
a **failure**, not skipped: a suite silently doing nothing is exactly the
outcome discovery is here to prevent. Import-time and in-test exceptions are
caught per suite so one broken module doesn't hide the results of the others.

No mailbox, no browser, no EXCHANGE_OWA_URL — importing the tool modules pulls
in `exchange_mcp.server` (and so Playwright's Python package), but nothing
launches a browser and no OWA URL is read at import time. Installing browsers
(`playwright install chromium`) is therefore *not* needed to run this.
"""

import importlib
import sys
import traceback
from pathlib import Path

PACKAGE = "tests.unit"


def discover() -> list[str]:
    """Every test module in this package, in a stable order."""
    here = Path(__file__).parent
    return sorted(path.stem for path in here.glob("test_*.py"))


def run_suite(name: str) -> tuple[bool, str]:
    """Import and run one suite. Returns (passed, note)."""
    try:
        module = importlib.import_module(f"{PACKAGE}.{name}")
    except Exception:
        traceback.print_exc()
        return False, "import failed"

    main = getattr(module, "main", None)
    if not callable(main):
        # Not a skip: a discovered module that can't be run is a hole in
        # coverage that looks like coverage.
        # ASCII only: this line is printed, and a piped stdout on Windows
        # encodes with the locale codepage, where a non-ASCII char raises.
        return False, "no callable main() - see tests/unit/__main__.py"

    try:
        result = main()
    except Exception:
        traceback.print_exc()
        return False, "raised"

    # A suite that forgets to return anything reads as falsy; say so rather
    # than reporting a failure it never had.
    if result is None:
        return False, "main() returned None - it must return True/False"
    return bool(result), ""


def main() -> bool:
    suites = discover()
    if not suites:
        print(f"No test_*.py modules found in {PACKAGE} — nothing ran.")
        return False

    print(f"Running {len(suites)} pure-logic suite(s) from {PACKAGE}\n")
    results: list[tuple[str, bool, str]] = []
    for name in suites:
        print(f"{'=' * 70}\n{name}\n{'=' * 70}")
        passed, note = run_suite(name)
        results.append((name, passed, note))
        print()

    print("=" * 70)
    print("Summary")
    print("=" * 70)
    for name, passed, note in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {name}{'  (' + note + ')' if note else ''}")

    failed = [name for name, passed, _ in results if not passed]
    print()
    if failed:
        print(f"{len(failed)} of {len(results)} suite(s) failed: {', '.join(failed)}")
        return False
    print(f"All {len(results)} suite(s) passed.")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
