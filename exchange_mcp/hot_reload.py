"""In-process reload of `exchange_mcp/tools/*.py` modules on a live `MCPServer`.

Lets a developer publish an edit to a tool module onto an already-running
`--transport http` server -- no restart, so the process's port stays bound and
its persistent browser session / OWA sign-in is untouched (see the "Hot
reload" section of CLAUDE.md for why that matters).

Pure logic, no Playwright import: takes the `MCPServer` instance and all
server-owned state as parameters rather than importing `exchange_mcp.server`,
both to stay unit-testable and to avoid an import cycle (`server.py` is what
calls into this module).

Scope is deliberately narrow: only modules in `MODULE_ALIASES` are ever passed
to `importlib.reload()`. Anything touching `OWAClient`/`BrowserSession`/other
shared globals still needs a real restart -- reload only re-executes the one
target module, not its dependencies.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

MODULE_ALIASES: dict[str, str] = {
    "email": "exchange_mcp.tools.email",
    "calendar": "exchange_mcp.tools.calendar",
    "people": "exchange_mcp.tools.people",
    "folders": "exchange_mcp.tools.folders",
    "availability": "exchange_mcp.tools.availability",
    "analytics": "exchange_mcp.tools.analytics",
    "auth": "exchange_mcp.tools.auth",
    "categories": "exchange_mcp.tools.categories",
    "copilot": "exchange_mcp.tools.copilot",
    "tasks": "exchange_mcp.tools.tasks",
    "discovery": "exchange_mcp.tools.discovery",
}


def resolve_module_name(raw: str, aliases: dict[str, str]) -> str | None:
    """Map a caller-supplied name to a reloadable dotted module name, or None.

    Accepts either the short alias (`"email"`) or the full dotted path
    (`"exchange_mcp.tools.email"`) -- but only ones present in `aliases`.
    Never returns an arbitrary caller-supplied dotted path: that is what keeps
    `importlib.reload()` from ever being handed something like `"os"`.
    """
    if raw in aliases:
        return aliases[raw]
    if raw in aliases.values():
        return raw
    return None


def _tools_of_module(mcp: "MCPServer", dotted_name: str) -> dict[str, Any]:
    return {
        t.name: t
        for t in mcp._tool_manager.list_tools()
        if getattr(t.fn, "__module__", None) == dotted_name
    }


def _remove_tools(mcp: "MCPServer", names: "list[str]") -> None:
    for name in names:
        try:
            mcp.remove_tool(name)
        except Exception:
            pass


def _restore_tools(mcp: "MCPServer", tools: dict[str, Any]) -> None:
    for old in tools.values():
        mcp.add_tool(
            fn=old.fn,
            name=old.name,
            title=old.title,
            description=old.description,
            annotations=old.annotations,
            icons=old.icons,
            meta=old.meta,
        )


def _reload_one(
    mcp: "MCPServer",
    dotted_name: str,
    *,
    stable_mode_active: bool,
    known_buggy_tools: dict[str, str],
) -> dict[str, Any]:
    before = _tools_of_module(mcp, dotted_name)
    _remove_tools(mcp, list(before.keys()))

    module = sys.modules.get(dotted_name)
    if module is None:
        try:
            module = importlib.import_module(dotted_name)
        except Exception as exc:
            _restore_tools(mcp, before)
            return {"status": "error", "error": str(exc), "restored": list(before.keys())}
        return {
            "status": "loaded",
            "added": sorted(_tools_of_module(mcp, dotted_name).keys() - before.keys()),
        }

    try:
        importlib.reload(module)
    except Exception as exc:
        # The reload may have partially re-registered some of the module's
        # tools before raising -- clear those before restoring the originals,
        # or add_tool's silent-keep-old-on-collision would leave the broken
        # partial registration in place instead of the restored one.
        partial = _tools_of_module(mcp, dotted_name)
        _remove_tools(mcp, list(partial.keys()))
        _restore_tools(mcp, before)
        return {"status": "error", "error": str(exc), "restored": list(before.keys())}

    after = _tools_of_module(mcp, dotted_name)
    stable_excluded: list[str] = []
    if stable_mode_active:
        for name in list(after.keys()):
            if name in known_buggy_tools:
                mcp.remove_tool(name)
                del after[name]
                stable_excluded.append(name)

    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    updated = sorted(
        name
        for name in (before.keys() & after.keys())
        if before[name].fn is not after[name].fn
    )
    return {
        "status": "ok",
        "added": added,
        "removed": removed,
        "updated": updated,
        "stable_excluded": stable_excluded,
    }


def reload_modules(
    mcp: "MCPServer",
    requested: "list[str] | None",
    *,
    stable_mode_active: bool = False,
    known_buggy_tools: "dict[str, str] | None" = None,
    module_aliases: "dict[str, str] | None" = None,
) -> dict[str, Any]:
    """Reload one or more tool modules on a live `MCPServer`.

    `requested=None` reloads every module in `module_aliases`. A name that
    doesn't resolve is rejected on its own -- it never aborts the rest of the
    batch, mirroring this repo's per-item batch-error convention.
    """
    aliases = module_aliases if module_aliases is not None else MODULE_ALIASES
    known_buggy_tools = known_buggy_tools or {}
    raw_requested = list(requested) if requested is not None else list(aliases.keys())

    modules: dict[str, Any] = {}
    changed = False
    for raw in raw_requested:
        dotted_name = resolve_module_name(raw, aliases)
        if dotted_name is None:
            modules[raw] = {"status": "rejected", "error": f"unknown reloadable module: {raw!r}"}
            continue
        result = _reload_one(
            mcp,
            dotted_name,
            stable_mode_active=stable_mode_active,
            known_buggy_tools=known_buggy_tools,
        )
        modules[dotted_name] = result
        if result["status"] in ("ok", "loaded") and (
            result.get("added") or result.get("removed") or result.get("updated")
        ):
            changed = True

    return {"requested": raw_requested, "modules": modules, "changed": changed}
