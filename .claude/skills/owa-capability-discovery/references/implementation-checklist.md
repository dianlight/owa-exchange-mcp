# Turning a discovery proposal into a working MCP tool

Read this only when the user has approved implementing something. Everything
below assumes you have already called `get_discovery_detail` for the endpoint and
are looking at its real captured payload.

## 1. Pick the transport from the evidence, not from taste

The captured record tells you which of the three helpers to use:

| Evidence in the capture | Helper | Notes |
|---|---|---|
| `header_payload: true` | `client.request_header_payload(action, payload)` | JSON travels URL-encoded in `X-OWA-UrlPostData`, body empty. Every `*_folder` tool and `categories.py` work this way. |
| Path is `/owa/service.svc`, plain body | `client.request(action, payload)` | The default EWS path. |
| Any other path (e.g. `/search/api/v1/...`, `/outlookgatewayb2/graphql`) | `client.request_substrate(path, headers, payload)` | **Modern bearer backend only.** Raises `BearerModeRequiredError` on classic canary-cookie OWA — catch it and fall back to an EWS equivalent if one exists, the way `find_person` does. |
| No API call at all for the feature | UI automation in `browser_session.py` | The `copilot_ask` / `_set_event_categories` escape hatch. Expensive and brittle — only when there is genuinely no endpoint. |

Reproduce the captured payload's shape faithfully, including `__type`
annotations. Per CLAUDE.md: `RequestServerVersion: "Exchange2013"` for reads,
`"V2017_08_18"` for writes.

## 2. Write the module or the tool

Match the existing modules — read [tasks.py](../../../exchange_mcp/tools/tasks.py)
first; it is the newest and most complete example.

- Module docstring explaining **why** the module works the way it does, and every
  non-obvious wire constraint discovered along the way. That docstring is where a
  future reader learns what cost you an afternoon.
- `_get_client(ctx)` pulling `OWAClient` off the lifespan context.
- Module-level `_READ_HEADER` / `_WRITE_HEADER` and a `_FIELD` dict for any
  write-side `FieldURI` spellings, so a live-test correction is one line. (The
  capability inventory reads these tables too, so hoisting them keeps future
  discovery reports accurate.)
- Every tool returns a **JSON string**, and catches `SessionExpiredError`
  separately from `Exception`.
- Validate arguments client-side and return an explanatory `error` rather than
  letting a bad `FieldURI` fail the whole request — one wrong spelling fails
  everything on this backend, and the error text is the only clue to which.
- A new module must be imported in [server.py](../../../exchange_mcp/server.py)
  next to the other `import exchange_mcp.tools.*` lines, or its tools never
  register.

## 3. Update the documentation in the same change

Not as a follow-up — CLAUDE.md is explicit about this.

- **PROJECT_STATUS.md**
  - Add a row per tool using the ID from the proposal. IDs are permanent and
    never reused or renumbered; a new module takes the next unused module number
    (the report already allocated both under that rule — re-check them if the
    file changed since the report was generated).
  - Update the module's tool count in its `###` heading and the
    "N tools across M modules" total in §3.
  - Set Manual QA to `Pending` until it has actually been run against a live
    mailbox, and `Stability` to `Stable` unless it's a confirmed unfixable fault.
  - If the endpoint was listed as a gap in §4, resolve that entry.
- **CLAUDE.md** — the tool total in "Structure", the `tools/` module list, and a
  short paragraph for a new module explaining its non-obvious constraints.
- **README.md** — the tool total and the tool table/list.
- **`server.json`** — only if the version changes (it can't read Python
  attributes, so both `version` fields are edited by hand).

## 4. Test it

- **Live behaviour** → a module under `tests/smoke/tests/`, following the
  existing lifecycle-test pattern (create → read → modify → delete, cleaning up
  after itself).
- **Pure logic** (date normalisation, validation, response mapping) → a module
  under `tests/unit/`, runnable as `python -m tests.unit.test_<name>`.
- Record the test module in PROJECT_STATUS.md's Automated test column.

## 5. Re-run discovery to confirm

Classification reads the live inventory, so once the tool is implemented:

```
classify_discovery_session(session_id="<same session>")
```

The endpoint should move from `unknown_api` to `known_api_covered`. Nothing is
re-recorded — this is a free check that the implementation actually covers what
was captured, and it catches the case where you implemented a *different* shape
than the one OWA sends.
