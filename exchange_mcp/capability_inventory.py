"""Static inventory of what this server *already* implements — the baseline
against which a capability-discovery capture is classified.

Pure logic: no Playwright, no live mailbox, no `EXCHANGE_OWA_URL`. It reads
this repository's own source with `ast` and PROJECT_STATUS.md with regexes,
so it can be unit-tested (`python -m tests.unit.test_capability_classify`)
and so it can never disagree with the code it describes.

**Why derived rather than hardcoded.** The obvious implementation is a dict
of "OWA actions we support". That dict is wrong the first time someone adds
a tool and forgets to update it, and a discovery report built on a stale
baseline reports things as *undiscovered* that were implemented last week —
the single most expensive failure mode this module can have. Scanning the
call sites (`client.request("FindItem", …)`) instead means the baseline is
whatever the code actually does.

Three deliberate imprecisions, each biased toward under-reporting rather
than over-reporting:

- **Payload keys are attributed to every action their enclosing function
  calls, plus everything the module declares at top level.** A function
  calling two actions over-credits both, and a module-level table is
  credited to every action in the file. That second rule is not laziness:
  this codebase deliberately hoists wire spellings out of the call site
  (`_FIELD` in [tools/tasks.py](tools/tasks.py), `_WRITE_HEADER`,
  `_ACTION` in [tools/categories.py](tools/categories.py)) so a live-test
  correction is one line — a scan that only looked inside the calling
  function would find *no* FieldURIs for `UpdateItem` and then report every
  one OWA sends as a discovery. Both rules err the same way: an
  over-credited baseline says "we already send that" (a missed finding,
  which the next capture surfaces again) rather than inventing work.
- **Only literal action names are seen.** `client.request(action_var, …)`
  where `action_var` is computed at runtime is invisible. Module-level
  string constants *are* resolved (`_ACTION = "GetMasterCategoryList"` in
  [tools/categories.py](tools/categories.py) is the real case), because
  that idiom is already in use here.
- **Action → tool attribution is by call site, not call graph.** An action
  is credited to the module it appears in and to the enclosing function's
  name, whether or not that function is itself an `@mcp.tool()`. Building a
  real call graph would buy precision nobody needs: the point is to tell a
  developer *where to look*, and "module + function that POSTs it" does
  that exactly.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

# Transport helpers whose first positional argument is an EWS action name.
# `request`/`request_header_payload` are OWAClient's; `post_json`/
# `post_header_payload` are BrowserSession's, called directly in a couple of
# places, and are listed so a call site can't hide by going one layer down.
_ACTION_CALLS = {
    "request": "request",
    "request_header_payload": "request_header_payload",
    "post_json": "request",
    "post_header_payload": "request_header_payload",
}

# Transport helpers whose first positional argument is a REST path on the
# modern Outlook origin (Substrate search, PeopleGraphVx, the GraphQL
# gateway) rather than an EWS action - see OWAClient.request_substrate.
_SUBSTRATE_CALLS = ("request_substrate", "post_substrate")

# `item:Subject`, `task:DueDate`, `calendar:Start`, ... Deliberately excludes
# `__type` values like `Task:#Exchange` (those start uppercase) and MIME types
# like `application/json` (no colon-then-uppercase).
_FIELD_URI_RE = re.compile(r"^[a-z][A-Za-z0-9]*:[A-Z][A-Za-z0-9]*$")

# A PROJECT_STATUS.md tool row: "| 1001 | `get_tasks` | ..."
_STATUS_ROW_RE = re.compile(r"^\|\s*(\d{3,4})\s*\|\s*`([^`]+)`")

# A PROJECT_STATUS.md module heading:
# "### Tasks — [exchange_mcp/tools/tasks.py](exchange_mcp/tools/tasks.py) (6)"
_STATUS_HEADING_RE = re.compile(r"^###\s+(.+?)\s+[—-]\s+\[([^\]]*?([A-Za-z_]+)\.py)\]")


@dataclass
class ActionCoverage:
    """Everything the static scan knows about one already-implemented endpoint."""

    name: str                                   # EWS action, or a REST path
    kind: str                                   # "ews_action" | "substrate"
    transports: set[str] = field(default_factory=set)
    modules: set[str] = field(default_factory=set)   # file stems: "email", "tasks", ...
    functions: set[str] = field(default_factory=set)  # enclosing def names
    payload_keys: set[str] = field(default_factory=set)
    field_uris: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "transports": sorted(self.transports),
            "modules": sorted(self.modules),
            "functions": sorted(self.functions),
            "payload_keys": sorted(self.payload_keys),
            "field_uris": sorted(self.field_uris),
        }


@dataclass
class Inventory:
    """The implemented baseline: endpoints, tools, and PROJECT_STATUS numbering state."""

    actions: dict[str, ActionCoverage] = field(default_factory=dict)
    substrate_paths: dict[str, ActionCoverage] = field(default_factory=dict)
    tools_by_module: dict[str, list[str]] = field(default_factory=dict)
    # PROJECT_STATUS.md state, for proposing IDs that obey CLAUDE.md's rule.
    modules_by_number: dict[int, dict] = field(default_factory=dict)
    ids_by_module: dict[int, list[int]] = field(default_factory=dict)
    tool_ids: dict[str, str] = field(default_factory=dict)  # tool name -> ID

    # ------------------------------------------------------------------
    # Lookups used by the classifier
    # ------------------------------------------------------------------

    def find_action(self, action: str) -> ActionCoverage | None:
        """Case-insensitive lookup - OWA echoes action names with varying case."""
        lowered = action.lower()
        for name, cov in self.actions.items():
            if name.lower() == lowered:
                return cov
        return None

    def find_substrate(self, path: str) -> ActionCoverage | None:
        """Match an observed REST path against the paths we already call.

        Prefix matching in both directions, because the implemented path
        carries a query string we strip (`/search/api/v1/suggestions`) while
        an observed one may be deeper (`/PeopleGraphVx/v1.0/peopleLookup`
        vs. an observed `/PeopleGraphVx/v1.0/peopleLookup/batch`).
        """
        observed = path.rstrip("/").lower()
        for known, cov in self.substrate_paths.items():
            k = known.rstrip("/").lower()
            if observed == k or observed.startswith(k + "/") or k.startswith(observed + "/"):
                return cov
        return None

    def module_number_for_file(self, file_stem: str) -> int | None:
        for number, info in self.modules_by_number.items():
            if info.get("file") == file_stem:
                return number
        return None

    def next_sequence(self, module_number: int) -> int:
        """Next unused 2-digit sequence in a module - append at the end of its max.

        Per CLAUDE.md: gaps left by removed tools are *not* reused, so this is
        max+1 rather than the lowest free slot.
        """
        used = self.ids_by_module.get(module_number, [])
        return (max(used) + 1) if used else 1

    def next_id(self, module_number: int) -> str:
        """Format the next permanent ID for a module (`1101`, `207`, ...)."""
        return f"{module_number}{self.next_sequence(module_number):02d}"

    def next_module_number(self) -> int:
        """Next unused module number - never a reused or renumbered one."""
        return (max(self.modules_by_number) + 1) if self.modules_by_number else 1

    def as_dict(self) -> dict:
        return {
            "actions": {k: v.as_dict() for k, v in sorted(self.actions.items())},
            "substrate_paths": {k: v.as_dict() for k, v in sorted(self.substrate_paths.items())},
            "tools_by_module": {k: sorted(v) for k, v in sorted(self.tools_by_module.items())},
            "modules_by_number": dict(sorted(self.modules_by_number.items())),
            "tool_count": sum(len(v) for v in self.tools_by_module.values()),
        }


# ----------------------------------------------------------------------
# Source scanning
# ----------------------------------------------------------------------


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments, so a constant can name an action.

    [tools/categories.py](tools/categories.py) does exactly this
    (`_ACTION = "GetMasterCategoryList"`), and a scan that only understood
    inline literals would report every category tool as unimplemented.
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
    return consts


def _literal_str(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.JoinedStr):
        # An f-string path like f"{base}/suggestions": keep the literal
        # fragments so prefix matching still has something to work with.
        parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts) or None
    return None


def _collect_payload_keys(node_or_nodes) -> tuple[set[str], set[str]]:
    """All dict keys and all FieldURI-shaped string values in a subtree.

    Returns (keys, field_uris). `__type` is dropped: it's a wire-format
    annotation present on every payload, so keeping it would make every
    observation look already-covered on that key.

    Accepts a single node (a function) or an iterable of them (a module's
    top-level assignments), because both are scanned - see the module
    docstring on why the module-level tables have to be included.
    """
    keys: set[str] = set()
    uris: set[str] = set()
    roots = node_or_nodes if isinstance(node_or_nodes, (list, tuple)) else [node_or_nodes]
    for root in roots:
        for node in ast.walk(root):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value != "__type":
                        keys.add(k.value)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _FIELD_URI_RE.match(node.value):
                    uris.add(node.value)
    return keys, uris


def _is_mcp_tool(func: ast.AST) -> bool:
    """True for a function carrying an `@mcp.tool()` decorator."""
    for dec in getattr(func, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _scan_module(path: Path, inventory: Inventory) -> None:
    """Add one source file's endpoints and tool names to the inventory."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return

    stem = path.stem
    consts = _module_string_constants(tree)

    # Wire spellings this module hoisted out of its call sites (`_FIELD`,
    # `_WRITE_HEADER`, ...). Credited to every action in the file - see the
    # module docstring.
    module_keys, module_uris = _collect_payload_keys(
        [node for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign))]
    )

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        if _is_mcp_tool(func) and stem != "server":
            inventory.tools_by_module.setdefault(stem, []).append(func.name)

        # Which endpoints does this function POST to, and via which transport?
        hits: list[tuple[str, str, str]] = []  # (kind, name, transport)
        for node in ast.walk(func):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
                continue
            attr = node.func.attr
            if attr in _ACTION_CALLS:
                name = _literal_str(node.args[0], consts)
                if name:
                    hits.append(("ews_action", name, _ACTION_CALLS[attr]))
            elif attr in _SUBSTRATE_CALLS:
                raw = _literal_str(node.args[0], consts)
                if raw:
                    hits.append(("substrate", raw.split("?")[0], "request_substrate"))

        if not hits:
            continue

        keys, uris = _collect_payload_keys(func)
        for kind, name, transport in hits:
            bucket = inventory.actions if kind == "ews_action" else inventory.substrate_paths
            cov = bucket.get(name)
            if cov is None:
                cov = ActionCoverage(name=name, kind=kind)
                bucket[name] = cov
            cov.transports.add(transport)
            cov.modules.add(stem)
            cov.functions.add(func.name)
            cov.payload_keys |= keys | module_keys
            cov.field_uris |= uris | module_uris


# ----------------------------------------------------------------------
# PROJECT_STATUS.md scanning
# ----------------------------------------------------------------------


def parse_project_status(text: str) -> tuple[dict[int, dict], dict[int, list[int]], dict[str, str]]:
    """Read the tool-inventory tables: module numbers, used IDs, tool -> ID.

    The module number is *derived from the IDs in each section*, not from a
    hardcoded table, so it keeps working when the next module is added.
    `<module><2-digit sequence>` splits unambiguously at the last two
    characters for both widths (`207` -> 2/07, `1001` -> 10/01).
    """
    modules: dict[int, dict] = {}
    ids: dict[int, list[int]] = {}
    tool_ids: dict[str, str] = {}

    current: dict | None = None
    for line in text.splitlines():
        heading = _STATUS_HEADING_RE.match(line)
        if heading:
            current = {"name": heading.group(1).strip(), "path": heading.group(2), "file": heading.group(3)}
            continue

        row = _STATUS_ROW_RE.match(line)
        if not row:
            continue
        raw_id, tool_name = row.group(1), row.group(2)
        module_number, sequence = int(raw_id[:-2]), int(raw_id[-2:])
        ids.setdefault(module_number, []).append(sequence)
        tool_ids[tool_name] = raw_id
        if current and module_number not in modules:
            modules[module_number] = dict(current)

    return modules, ids, tool_ids


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def repo_root() -> Path:
    """The directory holding `exchange_mcp/` - i.e. the source checkout root."""
    return Path(__file__).resolve().parent.parent


def build_inventory(root: Path | None = None) -> Inventory:
    """Scan this repository and return the implemented baseline.

    Cheap enough (a few dozen files) to run per classification rather than
    cached, which keeps a report generated after an edit honest about it.
    """
    root = Path(root) if root else repo_root()
    inventory = Inventory()

    package = root / "exchange_mcp"
    sources = sorted((package / "tools").glob("*.py")) + [package / "owa_client.py", package / "browser_session.py"]
    for source in sources:
        if source.exists():
            _scan_module(source, inventory)

    status = root / "PROJECT_STATUS.md"
    if status.exists():
        modules, ids, tool_ids = parse_project_status(status.read_text(encoding="utf-8"))
        inventory.modules_by_number = modules
        inventory.ids_by_module = ids
        inventory.tool_ids = tool_ids

    return inventory
