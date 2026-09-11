"""Classify a recorded OWA discovery session against what this server implements.

Pure logic — takes the JSONL records a `DiscoveryRecorder` wrote plus an
[Inventory](capability_inventory.py) of the implemented baseline, and answers
the three questions a capability sweep exists to answer:

1. **Is this an API we don't call at all?** (`unknown_api`) → a new tool.
2. **Is it one we call, but with parameters we never send?**
   (`known_api_new_parameters`) → extend an existing tool.
3. **Given the scope the user declared, is this a whole capability class we
   have no module for?** (`new_capability_class`) → a new tool module.

No Playwright, no live mailbox, no `EXCHANGE_OWA_URL`, so it's unit-testable
(`python -m tests.unit.test_capability_classify`) against recorded fixtures.

**The hint tables are hints.** `_COVERED_DOMAINS` and `_UNCOVERED_CLASSES`
below are keyword tables in the same spirit as the AADSTS/page-text tables in
[auth_errors.py](auth_errors.py): a deliberately-dumb lookup that is easy to
correct in one place when a live capture proves it wrong. They are the *only*
place domain knowledge lives, so a wrong classification is a one-line table
edit, never a code change. Nothing downstream treats their output as
authoritative — every finding carries the evidence (action name, path, keys,
call count) that produced it, so a reader can overrule the label.

**Why keys are compared as a flat set.** An observed request body is
flattened to the set of every key name it contains at any depth, and diffed
against the equally-flat key set the static scan collected from the
implementing function. Structure is thrown away, so a key we send under one
parent and OWA sends under another reads as "already covered". That is the
same under-report bias documented in `capability_inventory`: a missed finding
costs one more capture, a fabricated one costs a developer an afternoon.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from exchange_mcp.capability_inventory import Inventory

# Verdicts, in the order they're worth a developer's attention.
UNKNOWN_API = "unknown_api"
KNOWN_API_NEW_PARAMETERS = "known_api_new_parameters"
KNOWN_API_COVERED = "known_api_covered"

_VERDICT_RANK = {UNKNOWN_API: 0, KNOWN_API_NEW_PARAMETERS: 1, KNOWN_API_COVERED: 2}

# Domains this server already has a module for: file stem -> keywords matched
# against an observation's action name, URL path and the UI text around it.
_COVERED_DOMAINS: dict[str, tuple[str, ...]] = {
    "email": ("message", "mail", "inbox", "conversation", "reply", "forward",
              "attachment", "readflag", "junk", "sweep", "focused"),
    "calendar": ("calendar", "event", "meeting", "appointment", "recurrence",
                 "occurrence", "reminder", "roomlist", "room"),
    "categories": ("category", "categories", "mastercategorylist"),
    "people": ("persona", "people", "contact", "resolvenames", "directory",
               "gal", "peoplegraph"),
    "folders": ("folder", "mailboxfolder", "hierarchy"),
    "availability": ("availability", "freebusy", "getschedule", "workinghours",
                     "schedule", "suggestion"),
    "analytics": ("insight", "statistic", "analytics", "usage"),
    "auth": ("signin", "logon", "authentication", "canary", "token"),
    "copilot": ("copilot", "chat", "prompt", "substrateai", "augmentation"),
    "tasks": ("task", "todo", "to-do", "flaggeditem"),
}

# OWA capability classes this server has **no** module for. Each entry is
# (keywords, representative EWS/OWA action names) — the action names exist so a
# capture that shows one gets classified even when the URL is otherwise mute.
# Correct or extend this table when a live capture disagrees; do not push the
# knowledge into the matching code.
_UNCOVERED_CLASSES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "inbox_rules": (
        ("rule", "rules", "inboxrule", "condition", "moveto"),
        ("GetInboxRules", "UpdateInboxRules", "GetRules", "SetRules"),
    ),
    "out_of_office": (
        ("oof", "outofoffice", "automaticreply", "awaysetting", "vacation"),
        ("GetUserOofSettings", "SetUserOofSettings", "GetOofSettings"),
    ),
    "mailbox_settings": (
        ("useroption", "userconfiguration", "mailboxsetting", "language",
         "timezone", "workinghours", "signature", "accountinformation"),
        ("GetUserConfiguration", "UpdateUserConfiguration", "GetOwaUserConfiguration",
         "SetUserOptions", "GetUserSettings"),
    ),
    "delegates_sharing": (
        ("delegate", "sharing", "permission", "sharedmailbox", "folderpermission",
         "publishcalendar"),
        ("GetDelegate", "AddDelegate", "UpdateDelegate", "RemoveDelegate",
         "GetSharingMetadata", "SetCalendarSharingPermissions"),
    ),
    "notifications": (
        ("subscription", "subscribe", "notification", "streaming", "push", "hierarchysync"),
        ("SubscribeToNotification", "Subscribe", "GetStreamingEvents",
         "SyncFolderItems", "SyncFolderHierarchy"),
    ),
    "retention_archive": (
        ("retention", "archive", "policytag", "litigation", "compliance", "mrm"),
        ("GetRetentionPolicyTags", "SetItemRetentionPolicy", "ArchiveItem"),
    ),
    "groups": (
        ("unifiedgroup", "group", "team", "channel", "distributionlist"),
        ("GetUnifiedGroupDetails", "AddUnifiedGroupMembers", "FindUnifiedGroups",
         "ExpandDL"),
    ),
    "notes_contacts_crud": (
        ("note", "sticky", "contactgroup", "birthday"),
        ("CreateContact", "UpdateContact", "GetPersonaPhoto", "SetUserPhoto"),
    ),
    "search_refiners": (
        ("refiner", "searchquery", "suggestion", "querysuggestion", "searchhistory"),
        ("GetSearchSuggestions", "FindItemWithRefiners", "GetRefiners"),
    ),
}

_SCOPE_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "any", "all",
    "how", "what", "when", "does", "get", "set", "use", "using", "owa",
    "outlook", "exchange", "api", "apis", "feature", "features", "function",
    "functions", "support", "supported", "new", "check", "find", "look",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


# ----------------------------------------------------------------------
# Shared JSON helpers (also used by the recorder, hence public)
# ----------------------------------------------------------------------


def flatten_keys(value, *, _depth: int = 0) -> set[str]:
    """Every dict key name at any depth, minus the `__type` wire annotation.

    `__type` is on every EWS payload, so keeping it would make every single
    observation look already-covered on that one key.
    """
    keys: set[str] = set()
    if _depth > 12:
        return keys
    if isinstance(value, dict):
        for k, v in value.items():
            if k != "__type":
                keys.add(k)
            keys |= flatten_keys(v, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value[:5]:
            keys |= flatten_keys(item, _depth=_depth + 1)
    return keys


def field_uris_in(value, *, _depth: int = 0) -> set[str]:
    """FieldURI-shaped string values (`item:Subject`) anywhere in a payload."""
    from exchange_mcp.capability_inventory import _FIELD_URI_RE

    found: set[str] = set()
    if _depth > 12:
        return found
    if isinstance(value, dict):
        for v in value.values():
            found |= field_uris_in(v, _depth=_depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            found |= field_uris_in(item, _depth=_depth + 1)
    elif isinstance(value, str) and _FIELD_URI_RE.match(value):
        found.add(value)
    return found


def json_shape(value, *, _depth: int = 0):
    """A content-free skeleton of a JSON value: keys and types, no values.

    What the recorder stores instead of response bodies. Enough to write a
    response mapping against (you need the field names and whether something
    is a list), and it keeps mailbox content — subjects, names, addresses —
    out of the capture files entirely.
    """
    if _depth > 8:
        return "..."
    if isinstance(value, dict):
        return {k: json_shape(v, _depth=_depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        return [json_shape(value[0], _depth=_depth + 1), f"...x{len(value)}"] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "string"


# ----------------------------------------------------------------------
# Normalizing a capture into distinct endpoints
# ----------------------------------------------------------------------


def _endpoint_key(record: dict) -> tuple[str, str]:
    """Collapse a request record onto the endpoint it exercises.

    EWS traffic all shares one URL (`/owa/service.svc`) and differs only by
    `?action=`, so the action is the identity there. Everything else is
    identified by its path with the query string dropped.
    """
    action = (record.get("action") or "").strip()
    if action:
        return ("ews_action", action)
    path = (record.get("path") or "").split("?")[0]
    return ("substrate", path)


def normalize_capture(records: list[dict]) -> list[dict]:
    """Group raw network records into one observation per distinct endpoint."""
    observations: dict[tuple[str, str], dict] = {}

    for record in records:
        if record.get("type") != "request":
            continue
        kind, name = _endpoint_key(record)
        if not name:
            continue

        obs = observations.get((kind, name))
        if obs is None:
            obs = {
                "kind": kind,
                "name": name,
                "methods": set(),
                "urls": [],
                "calls": 0,
                "statuses": set(),
                "request_keys": set(),
                "field_uris": set(),
                "header_payload": False,
                "sample_request": None,
                "response_shape": None,
                "first_seen": record.get("at"),
                "ui_context": [],
            }
            observations[(kind, name)] = obs

        obs["calls"] += 1
        obs["methods"].add(record.get("method", "GET"))
        if record.get("url") and len(obs["urls"]) < 3 and record["url"] not in obs["urls"]:
            obs["urls"].append(record["url"])
        if record.get("status") is not None:
            obs["statuses"].add(record["status"])
        if record.get("header_payload"):
            obs["header_payload"] = True

        body = record.get("body")
        if isinstance(body, (dict, list)):
            obs["request_keys"] |= flatten_keys(body)
            obs["field_uris"] |= field_uris_in(body)
            if obs["sample_request"] is None:
                obs["sample_request"] = body
        if obs["response_shape"] is None and record.get("response_shape") is not None:
            obs["response_shape"] = record["response_shape"]

        hint = record.get("ui_hint")
        if hint and hint not in obs["ui_context"] and len(obs["ui_context"]) < 5:
            obs["ui_context"].append(hint)

    for obs in observations.values():
        obs["methods"] = sorted(obs["methods"])
        obs["statuses"] = sorted(s for s in obs["statuses"] if s is not None)
        obs["request_keys"] = sorted(obs["request_keys"])
        obs["field_uris"] = sorted(obs["field_uris"])

    return sorted(observations.values(), key=lambda o: -o["calls"])


# ----------------------------------------------------------------------
# Scope and domain matching
# ----------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if len(w) >= 3}


def scope_terms(scope: str) -> set[str]:
    """Meaningful words from the user's declared scope.

    Stopwords include the words that describe *this activity* ("api",
    "feature", "find") rather than its subject: a scope of "find any API for
    inbox rules" must reduce to {"inbox", "rules"}, or every observation
    matches and the in-scope flag stops meaning anything.
    """
    return _tokens(scope) - _SCOPE_STOPWORDS


def _observation_text(obs: dict) -> str:
    """One lowercase blob to keyword-match an observation against."""
    parts = [obs["name"], " ".join(obs["urls"]), " ".join(obs["ui_context"]),
             " ".join(obs["request_keys"][:40])]
    return " ".join(parts).lower()


def _score(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for kw in keywords if kw in text)


def classify_domain(obs: dict) -> tuple[str, str]:
    """Best-guess capability class. Returns (kind, name).

    kind is "covered" (an existing module's file stem), "new" (a key of
    `_UNCOVERED_CLASSES`), or "unclassified". An uncovered class wins ties:
    its keyword sets are narrower and more specific, so a hit there is
    stronger evidence than a hit on a broad word like "mail".
    """
    text = _observation_text(obs)
    action_lower = obs["name"].lower()

    best_new, best_new_score = None, 0
    for name, (keywords, actions) in _UNCOVERED_CLASSES.items():
        score = _score(text, keywords) + sum(3 for a in actions if a.lower() == action_lower)
        if score > best_new_score:
            best_new, best_new_score = name, score

    best_covered, best_covered_score = None, 0
    for name, keywords in _COVERED_DOMAINS.items():
        score = _score(text, keywords)
        if score > best_covered_score:
            best_covered, best_covered_score = name, score

    if best_new and best_new_score >= best_covered_score:
        return ("new", best_new)
    if best_covered:
        return ("covered", best_covered)
    return ("unclassified", "")


def _in_scope(obs: dict, terms: set[str], domain: tuple[str, str], scope_domains: set[str]) -> bool:
    """True when the observation plausibly belongs to the declared scope.

    Two independent routes, because a scope is written in the user's words and
    an action name is written in Microsoft's: a direct token hit in the
    observation text, or the observation landing in a capability class the
    *scope itself* matched.
    """
    if not terms:
        return True  # no scope declared - everything is in scope
    text = _observation_text(obs)
    if any(term in text for term in terms):
        return True
    return domain[1] in scope_domains


def _scope_domains(terms: set[str]) -> set[str]:
    """Which capability classes the scope text itself points at."""
    blob = " ".join(sorted(terms))
    matched = set()
    for name, keywords in _COVERED_DOMAINS.items():
        if _score(blob, keywords) or name in terms:
            matched.add(name)
    for name, (keywords, _actions) in _UNCOVERED_CLASSES.items():
        if _score(blob, keywords) or name in terms:
            matched.add(name)
    return matched


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------


def _suggested_transport(obs: dict) -> str:
    """Which OWAClient helper a new tool for this endpoint would call."""
    if obs["kind"] == "substrate":
        return "client.request_substrate(path, headers, payload)"
    if obs["header_payload"]:
        return 'client.request_header_payload("%s", payload)  # X-OWA-UrlPostData' % obs["name"]
    return 'client.request("%s", payload)' % obs["name"]


def _suggest_tool_name(obs: dict, domain: tuple[str, str]) -> str:
    """Propose a verb-first tool name in this codebase's style.

    EWS action names are already verb-first (`GetInboxRules`), so the
    conversion is a snake_case of the action; a REST path falls back to its
    last two segments.
    """
    if obs["kind"] == "ews_action":
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", obs["name"]).lower()
        return snake.replace("__", "_")
    segments = [s for s in obs["name"].split("/") if s and not s.startswith("v")]
    tail = "_".join(segments[-2:]) or "endpoint"
    return "get_" + re.sub(r"[^a-z0-9]+", "_", tail.lower()).strip("_")


def classify(
    records: list[dict],
    inventory: Inventory,
    *,
    scope: str = "",
    session_id: str = "",
    ui_actions: list[dict] | None = None,
) -> dict:
    """Turn one capture into findings and implementation proposals.

    `records` are the recorder's network JSONL rows; `ui_actions` the
    (optional) clicks/inputs it captured, used only for narrative and to give
    each endpoint a human-readable "what the user was doing" hint.
    """
    observations = normalize_capture(records)
    terms = scope_terms(scope)
    scoped_domains = _scope_domains(terms)

    findings: list[dict] = []
    for obs in observations:
        domain = classify_domain(obs)
        coverage = (
            inventory.find_action(obs["name"]) if obs["kind"] == "ews_action"
            else inventory.find_substrate(obs["name"])
        )

        if coverage is None:
            verdict = UNKNOWN_API
            new_keys = obs["request_keys"]
            new_uris = obs["field_uris"]
        else:
            new_keys = sorted(set(obs["request_keys"]) - coverage.payload_keys)
            new_uris = sorted(set(obs["field_uris"]) - coverage.field_uris)
            verdict = KNOWN_API_NEW_PARAMETERS if (new_keys or new_uris) else KNOWN_API_COVERED

        findings.append({
            "verdict": verdict,
            "kind": obs["kind"],
            "endpoint": obs["name"],
            "methods": obs["methods"],
            "calls": obs["calls"],
            "statuses": obs["statuses"],
            "in_scope": _in_scope(obs, terms, domain, scoped_domains),
            "capability_class": {"kind": domain[0], "name": domain[1]},
            "new_parameters": new_keys,
            "new_field_uris": new_uris,
            "existing_coverage": coverage.as_dict() if coverage else None,
            "suggested_transport": _suggested_transport(obs),
            "example_url": obs["urls"][0] if obs["urls"] else "",
            "ui_context": obs["ui_context"],
            "response_shape": obs["response_shape"],
            "sample_request": obs["sample_request"],
        })

    findings.sort(key=lambda f: (not f["in_scope"], _VERDICT_RANK[f["verdict"]], -f["calls"]))
    proposals = _build_proposals(findings, inventory)

    return {
        "session_id": session_id,
        "scope": scope,
        "scope_terms": sorted(terms),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "endpoints": len(findings),
            "unknown_api": sum(1 for f in findings if f["verdict"] == UNKNOWN_API),
            "known_api_new_parameters": sum(1 for f in findings if f["verdict"] == KNOWN_API_NEW_PARAMETERS),
            "known_api_covered": sum(1 for f in findings if f["verdict"] == KNOWN_API_COVERED),
            "in_scope": sum(1 for f in findings if f["in_scope"]),
            "network_records": len(records),
            "ui_actions": len(ui_actions or []),
        },
        "findings": findings,
        "proposals": proposals,
        "ui_narrative": _narrative(ui_actions or []),
        "baseline": {
            "known_actions": len(inventory.actions),
            "known_substrate_paths": len(inventory.substrate_paths),
            "known_tools": sum(len(v) for v in inventory.tools_by_module.values()),
        },
    }


def _build_proposals(findings: list[dict], inventory: Inventory) -> dict:
    """Turn findings into concrete "here's what to implement" records.

    Three shapes, matching the three verdicts. IDs follow CLAUDE.md's
    numbering rule: a new tool in an existing module takes that module's next
    unused sequence, and a new module takes the next unused module number —
    counted forward across proposals so two new modules in one report don't
    both claim the same number.
    """
    new_tools: list[dict] = []
    extend_tools: list[dict] = []
    new_modules: dict[str, dict] = {}

    # Track sequence allocation locally so several proposals in one report
    # don't all propose the same ID.
    allocated: dict[int, int] = {}

    def take_id(module_number: int) -> str:
        seq = allocated.get(module_number) or inventory.next_sequence(module_number)
        allocated[module_number] = seq + 1
        return f"{module_number}{seq:02d}"

    next_module = inventory.next_module_number()

    for finding in findings:
        cls_kind, cls_name = finding["capability_class"]["kind"], finding["capability_class"]["name"]

        if finding["verdict"] == KNOWN_API_NEW_PARAMETERS:
            coverage = finding["existing_coverage"] or {}
            module = (coverage.get("modules") or [""])[0]
            extend_tools.append({
                "endpoint": finding["endpoint"],
                "module": module,
                "module_file": f"exchange_mcp/tools/{module}.py" if module else "",
                "candidate_tools": inventory.tools_by_module.get(module, []),
                "implementing_functions": coverage.get("functions", []),
                "add_parameters": finding["new_parameters"],
                "add_field_uris": finding["new_field_uris"],
                "in_scope": finding["in_scope"],
                "calls": finding["calls"],
            })
            continue

        if finding["verdict"] != UNKNOWN_API:
            continue

        if cls_kind == "new" and cls_name:
            entry = new_modules.get(cls_name)
            if entry is None:
                entry = {
                    "capability_class": cls_name,
                    "proposed_module_file": f"exchange_mcp/tools/{cls_name}.py",
                    "proposed_module_number": next_module,
                    "endpoints": [],
                    "in_scope": False,
                }
                new_modules[cls_name] = entry
                next_module += 1
            entry["endpoints"].append({
                "endpoint": finding["endpoint"],
                "suggested_tool": _tool_name_for(finding),
                "proposed_id": f"{entry['proposed_module_number']}{len(entry['endpoints']) + 1:02d}",
                "transport": finding["suggested_transport"],
                "calls": finding["calls"],
            })
            entry["in_scope"] = entry["in_scope"] or finding["in_scope"]
            continue

        module = cls_name if cls_kind == "covered" else ""
        module_number = inventory.module_number_for_file(module) if module else None
        new_tools.append({
            "endpoint": finding["endpoint"],
            "suggested_tool": _tool_name_for(finding),
            "target_module": module or "(undecided — capability class unrecognised)",
            "target_module_file": f"exchange_mcp/tools/{module}.py" if module else "",
            "proposed_id": take_id(module_number) if module_number else "(assign once the module is chosen)",
            "transport": finding["suggested_transport"],
            "request_keys": finding["new_parameters"],
            "field_uris": finding["new_field_uris"],
            "in_scope": finding["in_scope"],
            "calls": finding["calls"],
        })

    return {
        "new_tools": new_tools,
        "extend_tools": extend_tools,
        "new_modules": sorted(new_modules.values(), key=lambda m: (not m["in_scope"], m["capability_class"])),
    }


def _tool_name_for(finding: dict) -> str:
    obs = {
        "kind": finding["kind"],
        "name": finding["endpoint"],
        "urls": [finding.get("example_url", "")],
        "ui_context": finding.get("ui_context", []),
        "request_keys": finding.get("new_parameters", []),
    }
    return _suggest_tool_name(obs, ("", ""))


def _narrative(ui_actions: list[dict]) -> list[str]:
    """Condense captured UI events into a readable "what the user did" list."""
    lines: list[str] = []
    for action in ui_actions:
        label = action.get("label") or action.get("selector") or action.get("tag") or "?"
        line = f"{action.get('kind', 'event')}: {label}"
        if action.get("url"):
            line += f"  [{action['url']}]"
        if not lines or lines[-1] != line:
            lines.append(line)
    return lines[:200]


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _fmt_list(values: list[str], limit: int = 12) -> str:
    if not values:
        return "—"
    shown = ", ".join(f"`{v}`" for v in values[:limit])
    return shown + (f" _(+{len(values) - limit} more)_" if len(values) > limit else "")


def render_markdown(report: dict) -> str:
    """Render a report as the Markdown summary the skill shows the user."""
    counts = report["counts"]
    out: list[str] = []
    out.append(f"# OWA capability discovery — {report.get('session_id', '?')}")
    out.append("")
    out.append(f"**Scope:** {report.get('scope') or '_(none declared)_'}  ")
    out.append(f"**Generated:** {report.get('generated_at', '')}  ")
    out.append(
        f"**Baseline:** {report['baseline']['known_actions']} EWS actions, "
        f"{report['baseline']['known_substrate_paths']} REST paths, "
        f"{report['baseline']['known_tools']} tools already implemented"
    )
    out.append("")
    out.append(
        f"Captured {counts['network_records']} API calls and {counts['ui_actions']} UI actions, "
        f"covering **{counts['endpoints']} distinct endpoints** "
        f"({counts['in_scope']} in scope)."
    )
    out.append("")
    out.append("| Verdict | Count |")
    out.append("|---|---|")
    out.append(f"| Unknown API (no tool calls it) | {counts['unknown_api']} |")
    out.append(f"| Known API, parameters we never send | {counts['known_api_new_parameters']} |")
    out.append(f"| Known and fully covered | {counts['known_api_covered']} |")
    out.append("")

    proposals = report["proposals"]

    if proposals["new_modules"]:
        out.append("## Proposed new tool modules (new capability classes)")
        out.append("")
        for module in proposals["new_modules"]:
            flag = " — **in scope**" if module["in_scope"] else ""
            out.append(
                f"### `{module['proposed_module_file']}` — module "
                f"**{module['proposed_module_number']}**, class `{module['capability_class']}`{flag}"
            )
            out.append("")
            out.append("| Proposed ID | Tool | Endpoint | Transport | Calls |")
            out.append("|---|---|---|---|---|")
            for endpoint in module["endpoints"]:
                out.append(
                    f"| {endpoint['proposed_id']} | `{endpoint['suggested_tool']}` | "
                    f"`{endpoint['endpoint']}` | `{endpoint['transport']}` | {endpoint['calls']} |"
                )
            out.append("")

    if proposals["new_tools"]:
        out.append("## Proposed new tools in existing modules")
        out.append("")
        out.append("| Proposed ID | Tool | Module | Endpoint | Transport | Scope | Calls |")
        out.append("|---|---|---|---|---|---|---|")
        for tool in proposals["new_tools"]:
            out.append(
                f"| {tool['proposed_id']} | `{tool['suggested_tool']}` | `{tool['target_module']}` | "
                f"`{tool['endpoint']}` | `{tool['transport']}` | "
                f"{'in scope' if tool['in_scope'] else 'incidental'} | {tool['calls']} |"
            )
        out.append("")

    if proposals["extend_tools"]:
        out.append("## Proposed changes to existing tools (new parameters observed)")
        out.append("")
        for change in proposals["extend_tools"]:
            out.append(
                f"- **`{change['endpoint']}`** in `{change['module_file'] or change['module']}` "
                f"({'in scope' if change['in_scope'] else 'incidental'}, {change['calls']} calls)"
            )
            out.append(f"  - implemented by: {_fmt_list(change['implementing_functions'])}")
            out.append(f"  - candidate tools: {_fmt_list(change['candidate_tools'])}")
            out.append(f"  - parameters OWA sends that we never do: {_fmt_list(change['add_parameters'])}")
            if change["add_field_uris"]:
                out.append(f"  - new FieldURIs: {_fmt_list(change['add_field_uris'])}")
        out.append("")

    out.append("## All observed endpoints")
    out.append("")
    out.append("| Verdict | Endpoint | Class | Calls | Status | New parameters |")
    out.append("|---|---|---|---|---|---|")
    for finding in report["findings"]:
        cls = finding["capability_class"]
        cls_label = f"{cls['kind']}:{cls['name']}" if cls["name"] else "unclassified"
        statuses = ", ".join(str(s) for s in finding["statuses"]) or "—"
        out.append(
            f"| {finding['verdict']} | `{finding['endpoint']}` | {cls_label} | "
            f"{finding['calls']} | {statuses} | {_fmt_list(finding['new_parameters'], 6)} |"
        )
    out.append("")

    if report["ui_narrative"]:
        out.append("## What the user did (captured UI actions)")
        out.append("")
        for line in report["ui_narrative"][:60]:
            out.append(f"- {line}")
        out.append("")

    out.append("---")
    out.append("")
    out.append(
        "_Verdicts come from a static scan of this repo's own call sites plus the keyword "
        "tables in `exchange_mcp/capability_classify.py`, and are biased toward "
        "under-reporting (see that module's docstring). Every row carries its evidence — "
        "overrule a label when the evidence says otherwise, and fix the table._"
    )
    return "\n".join(out)
