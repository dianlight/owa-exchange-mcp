"""Unit test: in-process reload of exchange_mcp/tools/*.py modules.

Needs no live mailbox, no browser and no EXCHANGE_OWA_URL. It does touch the
filesystem (real temp .py files), because `importlib.reload()` needs a module
with a real loader/spec -- a `types.ModuleType` built purely in memory doesn't
support reload, so fixtures are written to disk and imported/reloaded for
real, never faked.

Each test builds its own tiny fixture package under a `tempfile.TemporaryDirectory()`,
adds it to `sys.path`, and cleans up both `sys.path` and `sys.modules` afterwards so
one test's fixtures can never leak into another's (`tests/unit/__main__.py` imports
every suite in one process).

Run standalone:
    python -m tests.unit.test_hot_reload
"""

import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from exchange_mcp import hot_reload

# This suite rewrites fixture .py files and reloads them within the same
# process in rapid succession -- fast enough that two writes can land inside
# one tick of the filesystem's mtime granularity. A cached .pyc is validated
# against that timestamp, so without this a reload can silently serve last
# run's bytecode instead of the file just written (reproduced on NTFS).
sys.dont_write_bytecode = True

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


class _FixtureModule:
    """A real .py file on disk, imported for real, cleaned up for real."""

    def __init__(self, tmp: Path, mcp: MCPServer, module_name: str) -> None:
        self.tmp = tmp
        self.mcp = mcp
        self.module_name = module_name
        self.path = tmp / f"{module_name}.py"
        self._path_added = str(tmp) not in sys.path
        if self._path_added:
            sys.path.insert(0, str(tmp))

    def write(self, source: str) -> None:
        self.path.write_text(source, encoding="utf-8")

    def import_fresh(self):
        import importlib

        sys.modules.pop(self.module_name, None)
        # PathFinder caches a directory's file listing; this fixture's .py was
        # just written into a directory Python may have already scanned (e.g.
        # for hr_fixture_shared.py), and a write landing in the same
        # filesystem-timestamp tick as that scan makes the cache miss it --
        # reproduced flaky on NTFS. Force a re-scan before importing.
        importlib.invalidate_caches()
        return importlib.import_module(self.module_name)

    def close(self) -> None:
        sys.modules.pop(self.module_name, None)
        if self._path_added:
            try:
                sys.path.remove(str(self.tmp))
            except ValueError:
                pass


_SHARED_SOURCE = """
from mcp.server.mcpserver import MCPServer
mcp = MCPServer()
"""


def _make_shared(tmp: Path) -> None:
    (tmp / "hr_fixture_shared.py").write_text(_SHARED_SOURCE, encoding="utf-8")


def _load_shared_mcp(tmp: Path) -> MCPServer:
    import importlib

    sys.modules.pop("hr_fixture_shared", None)
    importlib.invalidate_caches()
    mod = importlib.import_module("hr_fixture_shared")
    return mod.mcp


# ------------------------------------------------------------------
# resolve_module_name
# ------------------------------------------------------------------


def test_resolve_module_name() -> None:
    print("resolve_module_name: alias, full path, and unknown")
    aliases = {"email": "exchange_mcp.tools.email"}
    check("short alias resolves", hot_reload.resolve_module_name("email", aliases) == "exchange_mcp.tools.email")
    check(
        "full dotted path resolves",
        hot_reload.resolve_module_name("exchange_mcp.tools.email", aliases) == "exchange_mcp.tools.email",
    )
    check("unknown name resolves to None", hot_reload.resolve_module_name("os", aliases) is None)


# ------------------------------------------------------------------
# Successful reload changes tool behavior
# ------------------------------------------------------------------


def test_successful_reload_swaps_behavior() -> None:
    print("A successful reload changes what the tool actually does")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sys.path.insert(0, tmp)
        try:
            _make_shared(tmp_path)
            mcp = _load_shared_mcp(tmp_path)
            fx = _FixtureModule(tmp_path, mcp, "hr_fixture_a")
            fx.write(
                "from hr_fixture_shared import mcp\n"
                "@mcp.tool()\n"
                "def widget() -> str:\n"
                "    return 'v1'\n"
            )
            fx.import_fresh()

            widget = next(t for t in mcp._tool_manager.list_tools() if t.name == "widget")
            check("v1 registered", widget.fn() == "v1", widget.fn())

            fx.write(
                "from hr_fixture_shared import mcp\n"
                "@mcp.tool()\n"
                "def widget() -> str:\n"
                "    return 'v2'\n"
            )
            aliases = {"fixture_a": "hr_fixture_a"}
            result = hot_reload.reload_modules(mcp, ["fixture_a"], module_aliases=aliases)

            check("reload reports changed", result["changed"] is True)
            mod_result = result["modules"]["hr_fixture_a"]
            check("reload status ok", mod_result["status"] == "ok", repr(mod_result))
            check("widget reported as updated", "widget" in mod_result["updated"], repr(mod_result))

            widget = next(t for t in mcp._tool_manager.list_tools() if t.name == "widget")
            check("v2 takes effect without reconnecting", widget.fn() == "v2", widget.fn())
            fx.close()
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("hr_fixture_shared", None)


# ------------------------------------------------------------------
# A broken reload rolls back to the previous working tools
# ------------------------------------------------------------------


def test_broken_reload_rolls_back() -> None:
    print("A reload that raises leaves the previous tools intact")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sys.path.insert(0, tmp)
        try:
            _make_shared(tmp_path)
            mcp = _load_shared_mcp(tmp_path)
            fx = _FixtureModule(tmp_path, mcp, "hr_fixture_b")
            fx.write(
                "from hr_fixture_shared import mcp\n"
                "@mcp.tool()\n"
                "def widget() -> str:\n"
                "    return 'v1'\n"
            )
            original = fx.import_fresh()
            original_fn = next(t for t in mcp._tool_manager.list_tools() if t.name == "widget").fn

            fx.write(
                "from hr_fixture_shared import mcp\n"
                "@mcp.tool()\n"
                "def widget() -> str:\n"
                "    return 'v2'\n"
                "raise RuntimeError('deliberately broken reload')\n"
            )
            aliases = {"fixture_b": "hr_fixture_b"}
            result = hot_reload.reload_modules(mcp, ["fixture_b"], module_aliases=aliases)

            mod_result = result["modules"]["hr_fixture_b"]
            check("reload status error", mod_result["status"] == "error", repr(mod_result))
            check("error names the raised exception", "deliberately broken" in mod_result["error"], repr(mod_result))
            check("widget listed as restored", "widget" in mod_result["restored"], repr(mod_result))

            tools_now = [t for t in mcp._tool_manager.list_tools() if t.name == "widget"]
            check("exactly one widget tool remains registered", len(tools_now) == 1, len(tools_now))
            check("the original v1 callable is still the active one", tools_now[0].fn is original_fn)
            check("calling it still returns v1", tools_now[0].fn() == "v1", tools_now[0].fn())
            fx.close()
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("hr_fixture_shared", None)


# ------------------------------------------------------------------
# --stable re-exclusion survives a reload
# ------------------------------------------------------------------


def test_stable_mode_reexcludes_buggy_tool() -> None:
    print("stable_mode_active strips a known-buggy tool back out after reload")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sys.path.insert(0, tmp)
        try:
            _make_shared(tmp_path)
            mcp = _load_shared_mcp(tmp_path)
            fx = _FixtureModule(tmp_path, mcp, "hr_fixture_c")
            fx.write(
                "from hr_fixture_shared import mcp\n"
                "@mcp.tool()\n"
                "def good_tool() -> str:\n"
                "    return 'good'\n"
                "@mcp.tool()\n"
                "def buggy_tool() -> str:\n"
                "    return 'buggy'\n"
            )
            fx.import_fresh()
            names_before = {t.name for t in mcp._tool_manager.list_tools()}
            check("both tools registered before reload", {"good_tool", "buggy_tool"} <= names_before)

            aliases = {"fixture_c": "hr_fixture_c"}
            result = hot_reload.reload_modules(
                mcp,
                ["fixture_c"],
                stable_mode_active=True,
                known_buggy_tools={"buggy_tool": "reproducible server-side bug"},
                module_aliases=aliases,
            )

            mod_result = result["modules"]["hr_fixture_c"]
            check("reload status ok", mod_result["status"] == "ok", repr(mod_result))
            check("buggy_tool reported as stable_excluded", "buggy_tool" in mod_result["stable_excluded"], repr(mod_result))

            names_after = {t.name for t in mcp._tool_manager.list_tools()}
            check("buggy_tool is not registered after reload", "buggy_tool" not in names_after, names_after)
            check("good_tool is still registered", "good_tool" in names_after, names_after)
            fx.close()
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("hr_fixture_shared", None)


# ------------------------------------------------------------------
# A disallowed module name is rejected, never imported
# ------------------------------------------------------------------


def test_disallowed_module_is_rejected() -> None:
    print("An arbitrary module name is rejected per-module, never reloaded")
    mcp = MCPServer()
    aliases = {"fixture_z": "hr_fixture_z_never_used"}
    result = hot_reload.reload_modules(mcp, ["os"], module_aliases=aliases)

    check("changed stays False", result["changed"] is False)
    mod_result = result["modules"]["os"]
    check("status is rejected", mod_result["status"] == "rejected", repr(mod_result))
    check("error names the module", "os" in mod_result["error"], repr(mod_result))


def main() -> bool:
    for test in (
        test_resolve_module_name,
        test_successful_reload_swaps_behavior,
        test_broken_reload_rolls_back,
        test_stable_mode_reexcludes_buggy_tool,
        test_disallowed_module_is_rejected,
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
