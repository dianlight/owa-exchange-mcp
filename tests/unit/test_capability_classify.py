"""Unit test: capability-discovery inventory, classification and capture safety.

Like the other tests under tests/unit/, this needs no live mailbox, no browser
and no EXCHANGE_OWA_URL -- everything under test is pure logic.

Three things are worth testing here, and they fail in different ways:

- **The inventory** is read out of this repo's own source. If it silently
  stopped finding call sites, every capture would report already-implemented
  actions as brand new, and the whole feature would generate busywork. So it's
  checked against actions this codebase definitely uses, including the one
  that hides behind a module-level constant.
- **The classifier's verdicts** decide what a developer is told to build. A
  false "unknown API" costs an afternoon; the tests below pin both directions.
- **Capture redaction** is a safety property, not a nicety: a discovery
  session always includes a live sign-in, so the login-host filter and the
  header allowlist are asserted directly rather than trusted.

Run standalone:
    python -m tests.unit.test_capability_classify
"""

import inspect
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from exchange_mcp import capability_classify as cc
from exchange_mcp import discovery_session as ds
from exchange_mcp.capability_classify import (
    KNOWN_API_COVERED,
    KNOWN_API_NEW_PARAMETERS,
    UNKNOWN_API,
    classify,
    classify_domain,
    field_uris_in,
    flatten_keys,
    json_shape,
    normalize_capture,
    render_markdown,
    scope_terms,
)
from exchange_mcp.capability_inventory import (
    ActionCoverage,
    Inventory,
    build_inventory,
    parse_project_status,
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {name}")
        return
    _failures.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  FAIL {name}{': ' + detail if detail else ''}")


# ----------------------------------------------------------------------
# Inventory: derived from this repo's own source
# ----------------------------------------------------------------------


def test_inventory_from_real_source() -> None:
    print("Inventory scan of this repository")
    inv = build_inventory()

    for action in ("FindItem", "GetItem", "CreateItem", "UpdateItem", "DeleteItem", "FindFolder"):
        check(f"finds the {action} call sites", inv.find_action(action) is not None)

    # categories.py names its action through a module-level constant
    # (_ACTION = "UpdateMasterCategoryList"). A scan that only read inline
    # literals would report every category tool as unimplemented.
    check("resolves an action named by a module-level constant",
          inv.find_action("UpdateMasterCategoryList") is not None)

    # Header-payload transport must be distinguishable from a plain POST -
    # it's what tells a proposal to use request_header_payload().
    create_folder = inv.find_action("CreateFolder")
    check("CreateFolder is recorded as a header-payload action",
          create_folder is not None and "request_header_payload" in create_folder.transports,
          str(create_folder.transports) if create_folder else "not found")

    check("finds the Substrate search path",
          inv.find_substrate("/search/api/v1/suggestions") is not None)
    check("matches a deeper observed path against a known prefix",
          inv.find_substrate("/search/api/v1/suggestions/extra") is not None)

    check("does not invent coverage for an action nobody calls",
          inv.find_action("GetInboxRules") is None)

    check("collects payload keys for a scanned action",
          "ItemShape" in (inv.find_action("FindItem").payload_keys or set()))
    # UpdateItem's FieldURIs live in tasks.py's module-level `_FIELD` table,
    # not inside the function that POSTs them - the exact indirection a
    # function-only scan misses, which would then report every FieldURI OWA
    # sends as newly discovered.
    check("collects FieldURIs hoisted into a module-level table",
          any(u.startswith("item:") for u in inv.find_action("UpdateItem").field_uris),
          str(sorted(inv.find_action("UpdateItem").field_uris)[:5]))

    check("attributes actions to their module", "email" in inv.find_action("FindItem").modules,
          str(inv.find_action("FindItem").modules))
    check("registers @mcp.tool() functions per module",
          "get_emails" in inv.tools_by_module.get("email", []))
    check("registers the discovery module's own tools",
          "start_discovery_session" in inv.tools_by_module.get("discovery", []))


def test_project_status_numbering() -> None:
    print("PROJECT_STATUS.md ID numbering (CLAUDE.md's rule)")
    text = """
### Email — [exchange_mcp/tools/email.py](exchange_mcp/tools/email.py) (2)

| ID | Tool | Description |
|---|---|---|
| 101 | `get_emails` | ... |
| 104 | `send_email` | ... |

### Tasks — [exchange_mcp/tools/tasks.py](exchange_mcp/tools/tasks.py) (1)

| ID | Tool | Description |
|---|---|---|
| 1001 | `get_tasks` | ... |
"""
    modules, ids, tool_ids = parse_project_status(text)
    inv = Inventory(modules_by_number=modules, ids_by_module=ids, tool_ids=tool_ids)

    check("splits a 3-digit ID into module 1", 1 in modules, str(sorted(modules)))
    check("splits a 4-digit ID into module 10", 10 in modules, str(sorted(modules)))
    check("maps module 10 to tasks.py", modules[10]["file"] == "tasks", str(modules.get(10)))
    check("maps a tool name to its permanent ID", tool_ids.get("get_tasks") == "1001")

    # Gaps left by removed tools are NOT reused: 101/104 -> next is 105, not 102.
    check("appends after a module's max sequence rather than filling a gap",
          inv.next_id(1) == "105", inv.next_id(1))
    check("keeps 4-digit width for a 2-digit module", inv.next_id(10) == "1002", inv.next_id(10))
    check("proposes the next unused module number", inv.next_module_number() == 11,
          str(inv.next_module_number()))
    check("resolves a file stem back to its module number",
          inv.module_number_for_file("tasks") == 10)


# ----------------------------------------------------------------------
# JSON helpers
# ----------------------------------------------------------------------


def test_json_helpers() -> None:
    print("Payload flattening, FieldURI extraction and response shaping")
    payload = {
        "__type": "UpdateItemJsonRequest:#Exchange",
        "Body": {
            "ItemChanges": [
                {"Updates": [{"Path": {"FieldURI": "item:Flag"}, "Item": {"Flag": {"FlagStatus": "Flagged"}}}]}
            ]
        },
    }
    keys = flatten_keys(payload)
    check("flattens nested keys", {"Body", "ItemChanges", "Updates", "Path", "FieldURI"} <= keys,
          str(sorted(keys)))
    check("drops the __type wire annotation", "__type" not in keys)
    check("extracts FieldURI values", field_uris_in(payload) == {"item:Flag"},
          str(field_uris_in(payload)))
    check("does not mistake a __type value for a FieldURI",
          "UpdateItemJsonRequest:#Exchange" not in field_uris_in(payload))

    shape = json_shape({"Subject": "Quarterly numbers", "Count": 3, "To": [{"Address": "a@b.c"}]})
    check("response shape keeps field names", set(shape) == {"Subject", "Count", "To"}, str(shape))
    check("response shape discards values", shape["Subject"] == "string" and shape["Count"] == "number",
          str(shape))
    check("response shape collapses a list to its element type",
          shape["To"][0] == {"Address": "string"}, str(shape["To"]))
    check("no mailbox content survives shaping", "Quarterly numbers" not in str(shape))


def test_normalize_capture() -> None:
    print("Grouping raw records into one observation per endpoint")
    records = [
        {"type": "request", "action": "FindItem", "method": "POST", "status": 200,
         "url": "https://owa/owa/service.svc?action=FindItem", "path": "/owa/service.svc",
         "body": {"Body": {"ItemShape": {"BaseShape": "IdOnly"}}}, "at": "t1"},
        {"type": "request", "action": "FindItem", "method": "POST", "status": 500,
         "url": "https://owa/owa/service.svc?action=FindItem", "path": "/owa/service.svc",
         "body": {"Body": {"Restriction": {}}}, "at": "t2"},
        {"type": "request", "action": "", "method": "POST", "status": 200,
         "url": "https://owa/PeopleGraphVx/v1.0/lookup?n=3", "path": "/PeopleGraphVx/v1.0/lookup",
         "body": {"Anchor": 1}, "at": "t3"},
    ]
    observations = normalize_capture(records)
    check("collapses same-action calls into one observation", len(observations) == 2,
          str([o["name"] for o in observations]))
    find_item = next(o for o in observations if o["name"] == "FindItem")
    check("counts repeated calls", find_item["calls"] == 2, str(find_item["calls"]))
    check("unions request keys across calls",
          {"ItemShape", "BaseShape", "Restriction"} <= set(find_item["request_keys"]),
          str(find_item["request_keys"]))
    check("records every observed status", find_item["statuses"] == [200, 500],
          str(find_item["statuses"]))
    check("identifies a non-EWS call by its path",
          any(o["name"] == "/PeopleGraphVx/v1.0/lookup" for o in observations))


# ----------------------------------------------------------------------
# Scope and domain matching
# ----------------------------------------------------------------------


def test_scope_terms() -> None:
    print("Scope tokenizing")
    terms = scope_terms("Find any OWA API for inbox rules and automatic replies")
    check("keeps the subject words", {"inbox", "rules", "automatic", "replies"} <= terms, str(terms))
    # Words describing the *activity* must go, or every observation matches the
    # scope and the in_scope flag stops carrying information.
    check("drops activity words", not ({"find", "any", "owa", "api", "for"} & terms), str(terms))


def test_domain_classification() -> None:
    print("Capability-class matching")
    rules = {"kind": "ews_action", "name": "GetInboxRules", "urls": [], "ui_context": [],
             "request_keys": []}
    check("an uncovered class is recognised by its action name",
          classify_domain(rules) == ("new", "inbox_rules"), str(classify_domain(rules)))

    oof = {"kind": "ews_action", "name": "SetUserOofSettings", "urls": [], "ui_context": [],
           "request_keys": ["OofSettings"]}
    check("out-of-office maps to its own class",
          classify_domain(oof) == ("new", "out_of_office"), str(classify_domain(oof)))

    mail = {"kind": "ews_action", "name": "FindConversation", "urls": ["/owa/service.svc"],
            "ui_context": ["Inbox"], "request_keys": ["ConversationShape"]}
    check("existing coverage maps to its module",
          classify_domain(mail) == ("covered", "email"), str(classify_domain(mail)))

    mystery = {"kind": "ews_action", "name": "Zzzyx", "urls": [], "ui_context": [], "request_keys": []}
    check("an unrecognisable endpoint stays unclassified",
          classify_domain(mystery)[0] == "unclassified", str(classify_domain(mystery)))


# ----------------------------------------------------------------------
# End-to-end classification
# ----------------------------------------------------------------------


def _synthetic_inventory() -> Inventory:
    """A small, fully-controlled baseline: one module, two actions, one REST path."""
    inv = Inventory()
    # `FieldURI` and `QueryString` are in the known-key sets on purpose: a real
    # scan of this repo finds both everywhere, so leaving them out would make
    # the fixture produce findings the live inventory never would.
    inv.actions["FindItem"] = ActionCoverage(
        name="FindItem", kind="ews_action", transports={"request"}, modules={"email"},
        functions={"get_emails"},
        payload_keys={"Body", "ItemShape", "BaseShape", "ParentFolderIds", "Traversal", "FieldURI"},
        field_uris={"item:Subject"},
    )
    inv.actions["GetItem"] = ActionCoverage(
        name="GetItem", kind="ews_action", transports={"request"}, modules={"email"},
        functions={"get_email"}, payload_keys={"Body", "ItemShape", "BaseShape", "ItemIds"},
    )
    inv.substrate_paths["/search/api/v1/suggestions"] = ActionCoverage(
        name="/search/api/v1/suggestions", kind="substrate", transports={"request_substrate"},
        modules={"people"}, functions={"find_people"},
        payload_keys={"Query", "QueryString", "EntityRequests"},
    )
    inv.tools_by_module = {"email": ["get_emails", "get_email"], "people": ["find_person"]}
    inv.modules_by_number = {1: {"name": "Email", "file": "email", "path": "x"}}
    inv.ids_by_module = {1: [1, 2]}
    return inv


def _records() -> list[dict]:
    return [
        # 1. An action nothing here calls, in a capability class with no module.
        {"type": "request", "action": "GetInboxRules", "method": "POST", "status": 200,
         "url": "https://owa/owa/service.svc?action=GetInboxRules", "path": "/owa/service.svc",
         "header_payload": True, "ui_hint": "Rules",
         "body": {"Body": {"MailboxSmtpAddress": "u@x.y"}}, "at": "t1"},
        # 2. An action we do call, carrying a request field we never send.
        {"type": "request", "action": "FindItem", "method": "POST", "status": 200,
         "url": "https://owa/owa/service.svc?action=FindItem", "path": "/owa/service.svc",
         "body": {"Body": {"ItemShape": {"BaseShape": "IdOnly"},
                           "Restriction": {"FieldURI": "item:IsRead"}}}, "at": "t2"},
        # 3. An action we call, with nothing new about it.
        {"type": "request", "action": "GetItem", "method": "POST", "status": 200,
         "url": "https://owa/owa/service.svc?action=GetItem", "path": "/owa/service.svc",
         "body": {"Body": {"ItemShape": {"BaseShape": "AllProperties"}, "ItemIds": []}}, "at": "t3"},
        # 4. A REST path we already use.
        {"type": "request", "action": "", "method": "POST", "status": 200,
         "url": "https://outlook.cloud.microsoft/search/api/v1/suggestions?n=1",
         "path": "/search/api/v1/suggestions", "body": {"Query": {"QueryString": "x"}}, "at": "t4"},
    ]


def test_classification_verdicts() -> None:
    print("Verdicts")
    report = classify(_records(), _synthetic_inventory(),
                      scope="inbox rules", session_id="20260911-120000-abcd")
    by_endpoint = {f["endpoint"]: f for f in report["findings"]}

    check("an uncalled action is unknown_api",
          by_endpoint["GetInboxRules"]["verdict"] == UNKNOWN_API,
          by_endpoint["GetInboxRules"]["verdict"])
    check("a called action with an extra field is known_api_new_parameters",
          by_endpoint["FindItem"]["verdict"] == KNOWN_API_NEW_PARAMETERS,
          by_endpoint["FindItem"]["verdict"])
    check("the extra field is named",
          "Restriction" in by_endpoint["FindItem"]["new_parameters"],
          str(by_endpoint["FindItem"]["new_parameters"]))
    check("a FieldURI we never send is reported",
          "item:IsRead" in by_endpoint["FindItem"]["new_field_uris"],
          str(by_endpoint["FindItem"]["new_field_uris"]))
    check("a fully-exercised action is known_api_covered",
          by_endpoint["GetItem"]["verdict"] == KNOWN_API_COVERED,
          by_endpoint["GetItem"]["verdict"])
    check("an already-used REST path is covered",
          by_endpoint["/search/api/v1/suggestions"]["verdict"] == KNOWN_API_COVERED,
          by_endpoint["/search/api/v1/suggestions"]["verdict"])

    check("counts add up", report["counts"]["endpoints"] == 4 and report["counts"]["unknown_api"] == 1,
          str(report["counts"]))
    check("the in-scope endpoint is flagged", by_endpoint["GetInboxRules"]["in_scope"] is True)
    check("an out-of-scope endpoint is not", by_endpoint["GetItem"]["in_scope"] is False,
          str(by_endpoint["GetItem"]["in_scope"]))
    check("the in-scope finding sorts first", report["findings"][0]["endpoint"] == "GetInboxRules",
          report["findings"][0]["endpoint"])


def test_classification_proposals() -> None:
    print("Proposals")
    report = classify(_records(), _synthetic_inventory(), scope="inbox rules")
    proposals = report["proposals"]

    check("proposes one new module", len(proposals["new_modules"]) == 1,
          str([m["capability_class"] for m in proposals["new_modules"]]))
    module = proposals["new_modules"][0]
    check("names the capability class", module["capability_class"] == "inbox_rules")
    check("takes the next unused module number", module["proposed_module_number"] == 2,
          str(module["proposed_module_number"]))
    check("allocates a permanent ID in that module",
          module["endpoints"][0]["proposed_id"] == "201", module["endpoints"][0]["proposed_id"])
    check("suggests a snake_case tool name",
          module["endpoints"][0]["suggested_tool"] == "get_inbox_rules",
          module["endpoints"][0]["suggested_tool"])
    check("carries the header-payload transport into the proposal",
          "request_header_payload" in module["endpoints"][0]["transport"],
          module["endpoints"][0]["transport"])
    check("flags the new module as in scope", module["in_scope"] is True)

    check("proposes extending exactly one existing tool set",
          len(proposals["extend_tools"]) == 1, str(proposals["extend_tools"]))
    change = proposals["extend_tools"][0]
    check("points at the right module", change["module"] == "email", change["module"])
    check("lists the module's tools as candidates",
          "get_emails" in change["candidate_tools"], str(change["candidate_tools"]))
    check("lists the parameter to add", change["add_parameters"] == ["Restriction"],
          str(change["add_parameters"]))
    check("makes no proposal for covered endpoints", not proposals["new_tools"],
          str(proposals["new_tools"]))


def test_no_scope_means_everything_in_scope() -> None:
    print("Empty scope")
    report = classify(_records(), _synthetic_inventory(), scope="")
    check("every finding is in scope when no scope is declared",
          all(f["in_scope"] for f in report["findings"]),
          str([(f["endpoint"], f["in_scope"]) for f in report["findings"]]))


def test_markdown_report() -> None:
    print("Markdown rendering")
    report = classify(_records(), _synthetic_inventory(), scope="inbox rules",
                      session_id="20260911-120000-abcd",
                      ui_actions=[{"kind": "click", "label": "Rules", "url": "https://owa/mail/options"}])
    md = render_markdown(report)
    for needle in ("OWA capability discovery", "inbox rules", "Proposed new tool modules",
                   "GetInboxRules", "All observed endpoints", "What the user did"):
        check(f"report contains {needle!r}", needle in md)
    check("report states the baseline it judged against", "already implemented" in md)


# ----------------------------------------------------------------------
# Capture safety (redaction is a requirement, not a nicety)
# ----------------------------------------------------------------------


def test_structural_keys_are_not_findings() -> None:
    """Body-rendering and shape-grammar keys must not read as discoveries.

    Live capture 20260911-112708-e917 scored `known_api_covered: 0` on 150
    endpoints because a real OWA read sends HTML-sanitisation options and
    restriction grammar we deliberately never send, so every known action came
    back as "new parameters". The two checks below pin both sides of that line:
    the grammar is dropped, but `Restriction` - which signals that OWA filters
    a read we don't - stays reportable.
    """
    print("Structural keys are not findings")

    records = _records() + [
        {"type": "request", "action": "GetItem", "method": "POST", "status": 200,
         "url": "https://owa/owa/service.svc?action=GetItem", "path": "/owa/service.svc",
         "body": {"Body": {"ItemShape": {
             "BaseShape": "IdOnly",
             # rendering options + extended-property grammar: all noise
             "FilterHtmlContent": True, "InlineImageUrlTemplate": "x",
             "CssScopeClassName": "y", "MaximumBodySize": 100,
             "PropertySetId": "guid", "PropertyName": "p", "PropertyType": "String",
             # ...and one key that genuinely names a capability
             "MaximumRecipientsToReturn": 10,
         }}}, "at": "t5"},
    ]
    report = classify(records, _synthetic_inventory(), scope="")
    by_endpoint = {f["endpoint"]: f for f in report["findings"]}

    new_keys = by_endpoint["GetItem"]["new_parameters"]
    for noise in ("FilterHtmlContent", "InlineImageUrlTemplate", "CssScopeClassName",
                  "MaximumBodySize", "PropertySetId", "PropertyName", "PropertyType"):
        check(f"{noise} is not reported as a new parameter",
              noise not in new_keys, str(new_keys))
    check("a genuine capability key survives the filter",
          "MaximumRecipientsToReturn" in new_keys, str(new_keys))

    # The other side of the line, so the table can't grow back over it.
    check("Restriction stays reportable",
          "Restriction" in by_endpoint["FindItem"]["new_parameters"],
          str(by_endpoint["FindItem"]["new_parameters"]))
    for container in ("Restriction", "SortOrder", "AdditionalProperties", "TimeZoneContext"):
        check(f"{container} is not listed as structural",
              container.lower() not in cc._STRUCTURAL_REQUEST_KEYS)

    check("a fully-exercised action can still reach known_api_covered",
          report["counts"]["known_api_covered"] >= 1,
          str(report["counts"]))


def test_capture_redaction() -> None:
    print("Capture redaction")
    for host in ("login.microsoftonline.com", "login.live.com", "contoso.b2clogin.com",
                 "adfs.contoso.com", "msauth.net"):
        check(f"{host} is treated as a sign-in host", ds._is_login_host(host))
    for host in ("outlook.cloud.microsoft", "owa.example.com", "outlook.office.com"):
        check(f"{host} is not treated as a sign-in host", not ds._is_login_host(host))

    lowered = {h.lower() for h in ds._HEADER_ALLOWLIST}
    for secret in ("authorization", "cookie", "x-owa-canary", "set-cookie", "x-csrf-token"):
        check(f"{secret} is never recorded", secret not in lowered)

    check("only fetch/xhr can be recorded as an API call",
          set(ds._API_RESOURCE_TYPES) == {"fetch", "xhr"}, str(ds._API_RESOURCE_TYPES))
    check("the injected page script never reads input values",
          ".value" not in ds._INIT_SCRIPT)

    # WebSocket frames on a Copilot socket *are* mailbox content (the prompt
    # and the generated answer), so they follow the same opt-in as HTTP bodies:
    # by default only direction and size reach disk.
    ws_source = inspect.getsource(ds.DiscoveryRecorder._on_websocket)
    check("websocket frame payloads are gated on capture_response_bodies",
          "self.capture_response_bodies" in ws_source)
    check("websocket sign-in traffic is dropped like HTTP sign-in traffic",
          "_is_login_host" in ws_source)
    check("websocket frame size is recorded unconditionally",
          '"size"' in ws_source)


def test_profile_cleanup() -> None:
    """Every session mints a Chromium profile, so cleanup is not optional.

    A leaked profile is tens of megabytes in the user's temp directory, and the
    normal teardown runs on a daemon thread that process exit can kill outright
    - which is exactly how the first live run of this recorder leaked one.
    """
    print("Temp-profile cleanup")
    fresh = Path(tempfile.mkdtemp(prefix=ds._PROFILE_PREFIX + "unit-fresh-"))
    (fresh / "Default").mkdir()
    (fresh / "Default" / "Cookies").write_text("x", encoding="utf-8")

    stale = Path(tempfile.mkdtemp(prefix=ds._PROFILE_PREFIX + "unit-stale-"))
    old = time.time() - (ds._STALE_PROFILE_AGE_SECONDS + 600)
    os.utime(stale, (old, old))

    unrelated = Path(tempfile.mkdtemp(prefix="not-a-discovery-profile-"))
    os.utime(unrelated, (old, old))

    try:
        ds.sweep_stale_profiles()
        check("sweeps an abandoned profile", not stale.exists())
        check("leaves a still-recent profile alone", fresh.exists())
        check("never touches unrelated temp directories", unrelated.exists())

        check("removes a populated profile directory", ds._remove_profile(fresh) is True)
        check("reports success for an already-absent directory",
              ds._remove_profile(fresh / "gone") is True)
    finally:
        for path in (fresh, stale, unrelated):
            shutil.rmtree(path, ignore_errors=True)


def main() -> bool:
    for test in (
        test_inventory_from_real_source,
        test_project_status_numbering,
        test_json_helpers,
        test_normalize_capture,
        test_scope_terms,
        test_domain_classification,
        test_classification_verdicts,
        test_classification_proposals,
        test_no_scope_means_everything_in_scope,
        test_markdown_report,
        test_structural_keys_are_not_findings,
        test_capture_redaction,
        test_profile_cleanup,
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
