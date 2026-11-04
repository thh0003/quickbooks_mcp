# QuickBooks Time & Invoice MCP Server Implementation Plan

Created: 2026-11-03
Author: tholmes4005@gmail.com
Agent: Claude Code
Status: PENDING
Approved: Yes
Iterations: 0
Worktree: No
Type: Feature

## Summary

**Goal:** From a Claude Code session, the user can log time for an employee or vendor against a QuickBooks Online customer and then generate a QuickBooks invoice that contains that customer's unbilled time, through a local Python MCP server registered with `claude mcp add`. Requirements: `docs/prd/2026-11-03-quickbooks-time-invoice-mcp.md`.

## Out of Scope

- Sending or emailing the created invoice, and any payment handling. The invoice is left in QBO for the user to review and send.
- Update and delete tools for time entries or invoices. Corrections happen in the QBO UI.
- Creating customers, employees, vendors, or service items. Lookup tools report "not found" instead.
- HTTP or remote transport, multiple companies, or multiple users. The server is a single-user stdio process for one realm.
- Automatic recovery when the refresh token has expired (100 days). The server reports that re-authorisation is required and the user re-runs the authorise command.

## Approach

**Chosen:** New `src/qbo_mcp/` package with three layers: `auth.py` (OAuth flow and token persistence via `intuit-oauth`), `qbo.py` (plain functions over a `python-quickbooks` client that do the QuickBooks work), and `server.py` (an `MCPServer` from the `mcp` 2.x SDK that exposes those functions as tools). A `qbo-mcp` console script with `authorize` and `serve` subcommands is the single entry point.
**Why:** Keeping QuickBooks logic out of the MCP layer lets the time-entry and invoice rules be unit-tested with a fake client that captures the exact JSON sent to QBO, and lets the MCP tools be tested in-process with the SDK's in-memory `Client`, at the cost of one extra module boundary. The three libraries are the ones Intuit's Python guidance names, so no HTTP code is hand-written.

## Global Constraints

- Python `>=3.10`; project managed with `uv` (`pyproject.toml` + `uv.lock`); virtual environment at `.venv/`.
- Dependencies: `mcp>=2.3,<3`, `python-quickbooks>=0.9.12`, `intuit-oauth>=1.2.6`, `python-dotenv>=1.0`, `pydantic>=2`. Dev: `pytest>=8`, `anyio>=4`.
- MCP high-level server import is `from mcp.server import MCPServer` and tool errors are `from mcp.server.mcpserver.exceptions import ToolError` (SDK 2.x renamed `FastMCP`).
- `QuickBooks(...)` is always constructed with `minorversion=75` and `use_decimal=True`.
- Nothing except the MCP SDK writes to stdout while serving; all logging goes to stderr via the `logging` module.
- Environment variables (read from `.env` via `python-dotenv`, process env wins): `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_ENVIRONMENT` (`sandbox` | `production`, default `sandbox`), `QBO_REDIRECT_URI` (default `http://localhost:8765/callback`), `QBO_TOKEN_PATH` (default `~/.qbo_mcp/tokens-<environment>.json`).
- Token file is written with mode `0600` and contains `access_token`, `refresh_token`, `realm_id`, `environment`, `saved_at` (ISO-8601 UTC).
- MCP server name: `quickbooks-time`. Tool names: `search_customers`, `search_workers`, `search_service_items`, `create_time_entry`, `list_time_entries`, `create_invoice_from_time`.
- Dates passed to tools are ISO `YYYY-MM-DD` strings; times are ISO-8601 with offset (e.g. `2026-11-03T09:00:00-05:00`).
- Invoice line description format: `"<TxnDate> <worker DisplayName>: <entry Description>"` (the trailing `: <Description>` is omitted when the entry has no description).

## Context for Implementer

QuickBooks Online (QBO) models a time entry as a `TimeActivity`. It must name exactly one worker: `NameOf` is `"Employee"` with `EmployeeRef`, or `"Vendor"` with `VendorRef`. Duration is either integer `Hours` + `Minutes` or `StartTime` + `EndTime` (with optional `BreakHours`/`BreakMinutes`), never both. `BillableStatus` is `Billable` or `NotBillable` on create; QBO itself sets `HasBeenBilled` when an invoice links the entry, and an app that tries to set it gets error 2170. Linking is done by placing `{"TxnId": "<TimeActivity.Id>", "TxnType": "TimeActivity"}` in `LinkedTxn` on the invoice line that bills that entry. Intuit's reference documents this as supported for create, but developer-forum reports disagree, so Task 8 confirms it against the sandbox and Task 4 carries the fallback switch (`mark_not_billable_on_invoice`). `python-quickbooks` raises `AuthorizationException` (QBO codes 1-499 or HTTP 401), `ValidationException` (2000-4999), `ObjectNotFoundException` (610), and `QuickbooksException` otherwise; `intuit-oauth` refresh failures propagate as `intuitlib.exceptions.AuthClientError`. Intuit rotates the refresh token on every refresh, so whatever is on `auth_client.refresh_token` after a request must be persisted or the next session's refresh will fail.

## Runtime Environment

- **Serve (stdio):** `uv run qbo-mcp serve` from the project root. Not meant to be run interactively; Claude Code launches it.
- **Register with Claude Code:** `claude mcp add quickbooks-time -- uv run --directory /Users/trevorholmes/local-git/quickbooks_mcp qbo-mcp serve`
- **Health check:** `claude mcp list` shows `quickbooks-time` as connected; inside a session `/mcp` lists the six tools.
- **Authorise:** `uv run qbo-mcp authorize` (prints a URL, waits on the local callback port, writes the token file).

## Assumptions

- The user has, or will create in Task 7, an Intuit Developer account with an app whose sandbox keys and a sandbox company are available. Tasks 7 and 8 depend on this.
- The sandbox company has the "Make Single-Time Activity Billable to Customer" preference enabled (Account and Settings → Advanced → Time tracking). Without it, QBO rejects `BillableStatus` on create and Task 8's end-to-end check cannot pass; Task 7 includes enabling it.
- `python-quickbooks` refreshes the access token in `_start_session()` only when `auth_client.access_token is None` and never retries a 401 on its own (per its configuration docs). Task 2's construct-with-`None` plus refresh-and-retry wrapper depends on this; if the installed `quickbooks/client.py` differs, keep the wrapper and adjust the construction accordingly.
- `python-quickbooks` object serialisation omits attributes whose value is `None` (so setting `LinkedTxn.TxnLineId = None` keeps it out of the payload). Task 4's payload test asserts this; if it fails, the implementer builds the `LinkedTxn` dict by hand in `qbo.py`.

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| QBO rejects or ignores `LinkedTxn` to `TimeActivity` on invoice lines, so entries stay "unbilled" after invoicing | Medium | High | `create_invoice_from_time` accepts `mark_not_billable_on_invoice` (default `False`). Task 8 runs the sandbox check; if the linked entries are not `HasBeenBilled` afterwards, Task 8 flips the default to `True` so entries are updated to `NotBillable` after the invoice is created, and documents the fallback in the README. |
| Refresh token rotation is lost (token file not rewritten after a refresh), breaking the next session | Medium | High | `auth.py` persists the token file after every `QuickBooks` request through a wrapper that compares `auth_client.refresh_token` to the stored value; Task 2 has a unit test that a rotated token is written to disk. |
| Access token expires mid-session (after one hour) and every tool call starts failing with "re-authorise" even though only a refresh is needed | High | High | Task 2 constructs the client with `access_token=None` so each process start refreshes, and the `make_request` wrapper refreshes once and retries on `AuthorizationException`; Task 2 has a unit test for the refresh-and-retry path, and Task 8's sandbox check is run a second time against the same server process after the first run to confirm no re-authorisation is required. |
| Server output corrupts the stdio protocol (stray prints or library logging to stdout) | Low | High | `logging.basicConfig(stream=sys.stderr)` in `server.py`; Task 5 includes a test that calling every tool in-process emits nothing on stdout. |

## Goal Verification

### Truths

1. After registration, a fresh Claude Code session can resolve a sandbox customer and worker by partial name, create a billable time entry, and then create an invoice for that customer; the invoice visible in the QBO sandbox UI contains that entry's hours, rate and description on its own line.
2. After an invoice has been created by the server, calling `list_time_entries` for the same customer with `billable_only=true` no longer returns the invoiced entries, and a second `create_invoice_from_time` call for the same range reports that there is no unbilled time and creates nothing.

## File Structure

- `pyproject.toml` (create) — uv project metadata, dependencies, `qbo-mcp` console script, pytest config.
- `.gitignore` (create) — ignores `.venv/`, `.env`, `*.json` token files, `__pycache__/`.
- `.env.example` (create) — documented copy of the five `QBO_*` variables with placeholder values.
- `README.md` (create) — setup, Intuit app creation, authorise, Claude Code registration, tool reference, sandbox check, troubleshooting.
- `src/qbo_mcp/__init__.py` (create) — package marker with `__version__`.
- `src/qbo_mcp/config.py` (create) — `Settings` dataclass loaded from `.env`/environment; validates required values and environment name.
- `src/qbo_mcp/auth.py` (create) — `TokenStore` (load/save 0600 JSON), `run_authorization_flow()` (consent URL + local callback), `build_quickbooks_client()` (constructs `AuthClient` + `QuickBooks`, persists rotated refresh tokens).
- `src/qbo_mcp/qbo.py` (create) — QuickBooks operations as plain functions taking a `QuickBooks` client: lookups, `create_time_entry`, `list_time_entries`, `create_invoice_from_time`, plus the result dataclasses they return.
- `src/qbo_mcp/server.py` (create) — `MCPServer("quickbooks-time")`, six tools delegating to `qbo.py`, exception-to-`ToolError` mapping, stderr logging.
- `src/qbo_mcp/__main__.py` (create) — argparse CLI: `authorize`, `serve`.
- `scripts/sandbox_check.py` (create) — end-to-end script against the sandbox: lookup, create entry, invoice, re-read entry, print billable status.
- `tests/conftest.py` (create) — `FakeQuickBooks` (records `query`/`create_object`/`update_object`/`get_single_object` calls, returns canned JSON) and shared fixtures.
- `tests/test_config.py` (create) — settings loading and validation.
- `tests/test_auth.py` (create) — token store, callback parsing, refresh-token rotation persistence.
- `tests/test_qbo_lookups.py` (create) — customer, worker and service-item search behaviour.
- `tests/test_qbo_time.py` (create) — time entry creation payloads and listing filters.
- `tests/test_qbo_invoice.py` (create) — unbilled sweep, line building, rate fallback, linking, fallback flag.
- `tests/test_server.py` (create) — in-memory MCP client calls for every tool, error mapping, stdout silence.
- `docs/prd/2026-11-03-quickbooks-time-invoice-mcp.md` (modify) — header `Status: Draft` → `Status: Final`.

## Progress Tracking

- [x] Task 1: Project scaffold, virtual environment, dependencies, PRD status
- [x] Task 2: Settings, token store, OAuth authorise flow, client factory
- [x] Task 3: QuickBooks lookups and time-entry operations
- [x] Task 4: Invoice generation from unbilled time
- [x] Task 5: MCP server, tools, error mapping, CLI entry point
- [x] Task 6: README, .env.example, sandbox check script
- [ ] Task 7: (User) Intuit app, sandbox company, authorise
- [ ] Task 8: Sandbox end-to-end check, link-behaviour decision, Claude Code registration

## Deviations

- Task 1 (tactical): `python-quickbooks` 0.9.12 pins `intuit-oauth==1.2.6`, so the planned `intuit-oauth>=1.2.7` floor was unsatisfiable → floor relaxed to `>=1.2.6` in `pyproject.toml` and `## Global Constraints`; the resolver picks 1.2.6.

- Task 6 (user-agreed): while paused at Task 7 the user asked for the public pages Intuit's production checklist requires → added `docs/site/index.html`, `docs/site/privacy.html`, `docs/site/terms.html`, `docs/site/disconnect.html`, `docs/site/style.css`, `.github/workflows/pages.yml`, and a "Production keys and the policy pages" section in `README.md`. Static HTML only; no runtime behaviour.

## Implementation Tasks

### Task 1: Project scaffold, virtual environment, dependencies, PRD status

**Objective:** Create the `uv`-managed Python project with the package skeleton, pinned dependencies, pytest configuration and ignore rules so every later task has a working environment and test runner. Also mark the approved PRD as Final.

**Files:**

- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/qbo_mcp/__init__.py`
- Modify: `docs/prd/2026-11-03-quickbooks-time-invoice-mcp.md`

**Key Decisions / Notes:**

- `pyproject.toml` uses the `hatchling` build backend with `packages = ["src/qbo_mcp"]`, `[project.scripts] qbo-mcp = "qbo_mcp.__main__:main"`, dependencies and dev group per `## Global Constraints`, and `[tool.pytest.ini_options] testpaths = ["tests"]`.
- `uv sync` creates `.venv/` and `uv.lock`; commit the lock file.
- `.gitignore` must cover `.venv/`, `.env`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, and `tokens-*.json`.
- `Trivial:` scaffold with no behaviour; verified by the import command in the DoD.

**Definition of Done:**

- [ ] `uv sync` completes and creates `.venv/` and `uv.lock`.
- [ ] `uv run python -c "import mcp, quickbooks, intuitlib, dotenv, qbo_mcp; from mcp.server import MCPServer"` exits 0.
- [ ] PRD header line reads `Status: Final`.
- [ ] Verify: `uv run python -c "import mcp, quickbooks, intuitlib, dotenv, qbo_mcp; from mcp.server import MCPServer; print('ok')"`

### Task 2: Settings, token store, OAuth authorise flow, client factory

**Objective:** Implement configuration loading, durable token storage, the one-time browser authorisation flow with a local callback listener, and a factory that returns a ready `QuickBooks` client which keeps the token file in sync when Intuit rotates the refresh token.

**Files:**

- Create: `src/qbo_mcp/config.py`
- Create: `src/qbo_mcp/auth.py`
- Create: `tests/conftest.py`
- Create: `tests/test_config.py`
- Create: `tests/test_auth.py`

**Key Decisions / Notes:**

- `config.py`: `Settings` dataclass with the five variables from `## Global Constraints`; `Settings.load(dotenv_path=None)` calls `dotenv.load_dotenv(override=False)` then reads `os.environ`; raises `ConfigError` naming every missing required variable in one message and rejecting an environment other than `sandbox`/`production`.
- `auth.py` `TokenStore(path)`: `load() -> Tokens | None`, `save(tokens)` writing JSON atomically (temp file + `os.replace`) and `os.chmod(path, 0o600)`; create parent directory on demand.
- `run_authorization_flow(settings, store, open_browser=True)`: build `AuthClient(client_id, client_secret, redirect_uri, environment)`, print `get_authorization_url([Scopes.ACCOUNTING])` to stderr, serve one request on the redirect URI's host/port with `http.server`, verify the `state` query parameter matches `auth_client.state_token`, call `get_bearer_token(code, realm_id=realm_id)`, save tokens. Reply to the browser with a short "You can close this tab" page. Fail with a clear message if the port is busy.
- `build_quickbooks_client(settings, store) -> QuickBooks`: load tokens or raise `NotAuthorizedError("run `qbo-mcp authorize`")`; construct `AuthClient` with `access_token=None` (deliberately: `python-quickbooks` calls `auth_client.refresh(refresh_token=...)` during `_start_session()` only when the access token is `None`, so every process start obtains a fresh one-hour access token), then `QuickBooks(auth_client=..., refresh_token=tokens.refresh_token, company_id=realm_id, minorversion=75, use_decimal=True)`. Persist immediately after construction because the refresh rotated the refresh token.
- Long sessions: the server process is cached for the life of a Claude Code session, which can exceed the one-hour access-token lifetime, and `python-quickbooks` does not refresh on a 401 by itself. Wrap the client's `make_request` (pattern: `QuickBooks.make_request` in the installed `quickbooks/client.py`) so that on `AuthorizationException` it calls `auth_client.refresh(refresh_token=auth_client.refresh_token)` once, persists the rotated tokens via `TokenStore`, and retries the request once; a second `AuthorizationException` or an `AuthClientError` from the refresh propagates (mapped by Task 5 to the "run authorize" message). After every successful call, if `auth_client.refresh_token` or `auth_client.access_token` differs from what the store holds, rewrite the store.
- Tests use a `FakeAuthClient` (no network) and `tmp_path`; `conftest.py` also defines `FakeQuickBooks` for Tasks 3-4 with recorded `query(select)`, `create_object(name, json_body)`, `update_object(name, json_body)`, `get_single_object(name, pk)` and canned JSON responses, matching the method names in the installed `quickbooks/client.py`.

**Definition of Done:**

- [ ] `Settings.load()` with an empty environment raises `ConfigError` listing `QBO_CLIENT_ID` and `QBO_CLIENT_SECRET`; with `QBO_ENVIRONMENT=prod` it raises naming the valid values; defaults for redirect URI and token path are applied.
- [ ] `TokenStore.save()` produces a file with mode `0600` whose `load()` round-trips all five fields; `load()` on a missing file returns `None`.
- [ ] Callback handling rejects a mismatched `state` and never saves tokens; a matching `state` with `code` and `realmId` saves `realm_id` and both tokens.
- [ ] After a simulated refresh that changes `auth_client.refresh_token`, the token file on disk contains the new refresh token.
- [ ] With a `FakeAuthClient` whose first request raises `AuthorizationException` and whose `refresh()` issues new tokens, the wrapped `make_request` refreshes once, retries, returns the second response, and the store holds the new tokens; when `refresh()` raises `AuthClientError` the error propagates and the store is unchanged.
- [ ] `build_quickbooks_client()` without a token file raises `NotAuthorizedError`.
- [ ] Verify: `uv run pytest tests/test_config.py tests/test_auth.py -q`

### Task 3: QuickBooks lookups and time-entry operations

**Objective:** Implement the QuickBooks-facing functions for finding customers, workers and service items by partial name, creating a time entry for an employee or vendor against a customer, and listing time entries for a customer with date and billable filters. Each function takes a `QuickBooks` client and returns plain dataclasses that the MCP layer can serialise.

**Files:**

- Create: `src/qbo_mcp/qbo.py`
- Create: `tests/test_qbo_lookups.py`
- Create: `tests/test_qbo_time.py`

**Key Decisions / Notes:**

- Result dataclasses: `CustomerMatch(id, display_name, company_name, active)`, `WorkerMatch(id, display_name, kind)` with `kind` in `{"Employee","Vendor"}`, `ItemMatch(id, name, unit_price, type)`, `TimeEntry(id, txn_date, worker_kind, worker_id, worker_name, customer_id, customer_name, item_id, item_name, hours, minutes, hourly_rate, billable_status, description)`.
- Searches use `Customer.where("Active = true AND DisplayName LIKE '%<q>%'", max_results=25, qb=qb)` with `'` in the query escaped as `\'`; workers run the same query against `Employee` and `Vendor` and merge; items query `Item.where("Active = true AND Type = 'Service' AND Name LIKE '%<q>%'", ...)`. An empty query lists the first 25 active records.
- `create_time_entry(qb, *, worker_kind, worker_id, customer_id, item_id, txn_date, hours=None, minutes=None, start_time=None, end_time=None, hourly_rate=None, description=None, billable=True)`: raise `ValueError` unless exactly one of (`hours`/`minutes`) or (`start_time`/`end_time`) is given, and if `hours`+`minutes` is zero or negative. Set `NameOf`, the matching `EmployeeRef`/`VendorRef` via `Ref` with `type` and `value`, `CustomerRef`, `ItemRef`, `TxnDate`, `BillableStatus = "Billable"|"NotBillable"`, `HourlyRate`, `Description`, then `TimeActivity.save(qb=qb)`; map the saved object to `TimeEntry` (see `quickbooks/objects/timeactivity.py` in the installed package for attribute names).
- `list_time_entries(qb, *, customer_id, start_date=None, end_date=None, billable_only=False)`: `TimeActivity.where("CustomerRef = '<id>'[ AND TxnDate >= '<start>'][ AND TxnDate <= '<end>']", order_by="TxnDate", start_position=n, max_results=1000, qb=qb)` paged until a page returns fewer than 1000; `billable_only` keeps only `BillableStatus == "Billable"` client-side (filterability of that field is not documented). Duration for `StartTime`/`EndTime` entries without `Hours` is computed from the timestamps minus break time, so `TimeEntry.hours`/`minutes` are always populated.
- Worker names on entries: QBO returns `EmployeeRef.name`/`VendorRef.name`; use that, do not issue extra lookups.

**Definition of Done:**

- [ ] `search_workers(qb, "sam")` returns matches from both Employee and Vendor queries with the correct `kind`, and `search_customers(qb, "O'Brien")` sends a query with the apostrophe escaped.
- [ ] `create_time_entry` for an employee sends a `TimeActivity` body with `NameOf == "Employee"`, `EmployeeRef.value`, no `VendorRef`, `Hours`/`Minutes`, `BillableStatus == "Billable"`; for a vendor it sends `NameOf == "Vendor"` and `VendorRef`; `billable=False` sends `NotBillable`.
- [ ] `create_time_entry` raises `ValueError` when both duration forms are given, when neither is given, and when the duration is zero.
- [ ] `list_time_entries` builds the `CustomerRef`/`TxnDate` where clause exactly as specified, pages when a page has 1000 rows, and `billable_only=True` drops `HasBeenBilled` and `NotBillable` rows.
- [ ] Verify: `uv run pytest tests/test_qbo_lookups.py tests/test_qbo_time.py -q`

### Task 4: Invoice generation from unbilled time

**Objective:** Implement `create_invoice_from_time`, which sweeps a customer's unbilled billable time (optionally within a date range), builds one invoice line per entry with the rate taken from the entry or the service item, links each entry to its line, saves the invoice, and returns what was billed. Include the fallback switch that marks entries `NotBillable` after invoicing for the case where QBO ignores the link.

**Files:**

- Create: `tests/test_qbo_invoice.py`
- Modify: `src/qbo_mcp/qbo.py`

**Key Decisions / Notes:**

- Signature: `create_invoice_from_time(qb, *, customer_id, start_date=None, end_date=None, mark_not_billable_on_invoice=False) -> InvoiceResult | NoUnbilledTime`. `InvoiceResult(invoice_id, doc_number, txn_date, total, customer_name, lines=[InvoiceLineResult(time_entry_id, description, hours, rate, amount)])`; `NoUnbilledTime(customer_id, start_date, end_date)` is returned, not raised, so the tool can report it as a normal outcome.
- Reuse `list_time_entries(..., billable_only=True)` from Task 3 for the sweep.
- Per line: `SalesItemLine` with `Description` per the format in `## Global Constraints`, `SalesItemLineDetail.ItemRef = Ref(value=item_id, type="Item")`, `Qty = hours + minutes/60` rounded to 2 decimals, `UnitPrice` = entry `HourlyRate` if set and > 0, else `Item.get(item_id, qb=qb).UnitPrice` (cache per item id within the call), `Amount = round(Qty * UnitPrice, 2)`; `line.LinkedTxn = [LinkedTxn(TxnId=entry.id, TxnType="TimeActivity")]` with `TxnLineId = None` (see `## Assumptions`).
- Before saving, raise `InvoiceBuildError` listing every entry that has no `ItemRef`, or whose rate resolves to `None`/`0`, so the user can fix those entries in QBO; never save a partial invoice.
- `invoice.CustomerRef = Ref(value=customer_id, type="Customer")`; `invoice.save(qb=qb)`; map the response.
- When `mark_not_billable_on_invoice` is `True`, after a successful save re-fetch each entry with `TimeActivity.get(id, qb=qb)`, set `BillableStatus = "NotBillable"`, and `save(qb=qb)` (a full update needs the current `SyncToken`, which `get` provides).

**Definition of Done:**

- [ ] With two unbilled entries (one with `HourlyRate` 120, one without, whose item has `UnitPrice` 95) the saved invoice body has two `SalesItemLineDetail` lines with `Qty` 1.5 and 2.0, `UnitPrice` 120 and 95, `Amount` 180 and 190, descriptions in the required format, and each line's `LinkedTxn` is `[{"TxnId": "<id>", "TxnType": "TimeActivity"}]` with no `TxnLineId` key.
- [ ] With no billable entries in range the function returns `NoUnbilledTime` and `create_object` is never called.
- [ ] An entry with no item, or whose rate resolves to zero, raises `InvoiceBuildError` naming the entry id and `create_object` is never called.
- [ ] With `mark_not_billable_on_invoice=True`, after the invoice is created each entry is updated with `BillableStatus == "NotBillable"`; with the default `False` no `update_object` call is made.
- [ ] `start_date`/`end_date` are forwarded to the sweep query.
- [ ] Verify: `uv run pytest tests/test_qbo_invoice.py -q`

### Task 5: MCP server, tools, error mapping, CLI entry point

**Objective:** Expose the Task 3 and Task 4 functions as six MCP tools on an `MCPServer` with typed arguments and structured results, translate QuickBooks and authorisation failures into actionable `ToolError` messages, and provide the `qbo-mcp authorize|serve` command-line entry point.

**Files:**

- Create: `src/qbo_mcp/server.py`
- Create: `src/qbo_mcp/__main__.py`
- Create: `tests/test_server.py`

**Key Decisions / Notes:**

- `server.py`: `mcp = MCPServer("quickbooks-time")`; a lazily-built module-level client via `get_client()` that calls `build_quickbooks_client(Settings.load(), TokenStore(...))` once and caches it; tools are plain functions decorated with `@mcp.tool()` whose docstrings tell Claude when to use them, with `pydantic.Field(description=...)` on arguments; return the Task 3/4 dataclasses (the SDK derives output schemas from dataclasses).
- Tool signatures: `search_customers(query: str = "")`, `search_workers(query: str = "")`, `search_service_items(query: str = "")`, `create_time_entry(worker_kind, worker_id, customer_id, item_id, txn_date, hours=None, minutes=None, start_time=None, end_time=None, hourly_rate=None, description=None, billable=True)`, `list_time_entries(customer_id, start_date=None, end_date=None, billable_only=False)`, `create_invoice_from_time(customer_id, start_date=None, end_date=None)`. The fallback flag is not a tool argument; `server.py` reads a module constant `MARK_NOT_BILLABLE_ON_INVOICE = False` that Task 8 may flip.
- Error mapping in one decorator applied to every tool: `NotAuthorizedError`/`AuthClientError`/`AuthorizationException` → `ToolError("QuickBooks authorisation expired or missing. Run `uv run qbo-mcp authorize` and retry.")`; `ValidationException` → `ToolError(f"QuickBooks rejected the request: {e.message} {e.detail}")`; `ObjectNotFoundException` → `ToolError("... not found in QuickBooks")`; `ValueError`/`InvoiceBuildError` → `ToolError(str(e))`; `ConfigError` → `ToolError` naming the missing variables.
- `logging.basicConfig(level=INFO, stream=sys.stderr)` at import; `main()` in `__main__.py` parses `authorize` (runs Task 2 flow) and `serve` (calls `mcp.run()`, stdio default).
- Tests: `async with Client(mcp, raise_exceptions=False)` (`from mcp import Client`); monkeypatch `server.get_client` to return a `FakeQuickBooks` or monkeypatch the `qbo` functions directly; `pytest.mark.anyio` with an `anyio_backend` fixture returning `"asyncio"` (pattern from the MCP SDK 2.x testing guide).
- The `@mcp.tool()` decorator, dataclass/Pydantic return types producing an output schema, `ToolError` (surfaces as `is_error=True`), and the in-memory `Client` all come from the SDK 2.x docs (`docs/servers/structured-output.md`, `docs/servers/handling-errors.md`, `docs/get-started/testing.md`). Before writing `server.py`, confirm them against the installed package (`uv run python -c "import mcp, mcp.server, mcp.server.mcpserver.exceptions as e; print(mcp.__version__, e.ToolError); from mcp import Client"`) and adjust names if the installed version differs.

**Definition of Done:**

- [ ] `list_tools` returns exactly the six names from `## Global Constraints`, each with a non-empty description and an input schema that marks `customer_id` required on `list_time_entries` and `create_invoice_from_time`.
- [ ] Calling `create_time_entry` through the in-memory client returns `structured_content` with the new entry id and billable status; calling it with both duration forms returns `is_error=True` and the `ValueError` message.
- [ ] When `get_client` raises `NotAuthorizedError`, every tool returns `is_error=True` with a message containing `qbo-mcp authorize`.
- [ ] A `ValidationException` from the fake client surfaces as `is_error=True` with the QBO message and detail text.
- [ ] Calling all six tools in-process writes nothing to `sys.stdout` (captured via `capsys`).
- [ ] `uv run qbo-mcp --help` lists `authorize` and `serve`.
- [ ] Verify: `uv run pytest tests/test_server.py -q && uv run qbo-mcp --help`

### Task 6: README, .env.example, sandbox check script

**Objective:** Document setup end to end and provide a runnable script that exercises the real sandbox so Task 8 can verify the link behaviour and the user can repeat the check later.

**Files:**

- Create: `README.md`
- Create: `.env.example`
- Create: `scripts/sandbox_check.py`

**Key Decisions / Notes:**

- README sections: Prerequisites (Python 3.10+, uv, Intuit developer account); Create the Intuit app (where to find Client ID/Secret, add `http://localhost:8765/callback` as a redirect URI under the sandbox keys, enable the billable-time preference in the sandbox company); Install (`uv sync`); Configure (`.env`); Authorise (`uv run qbo-mcp authorize`); Register with Claude Code (the exact command from `## Runtime Environment`); Tools (one table row per tool with arguments and what it returns); Example conversation; Sandbox check (`uv run python scripts/sandbox_check.py`); Troubleshooting (expired refresh token after 100 days, port in use, `BillableStatus` rejected because the preference is off, switching to production).
- `scripts/sandbox_check.py` uses `Settings`, `TokenStore`, `build_quickbooks_client`, and the `qbo.py` functions directly: refuse to run unless `QBO_ENVIRONMENT=sandbox`; pick the first active customer, the first employee and the first service item (or accept `--customer`, `--worker`, `--item` name filters); create a 1h30m billable entry dated today with a unique description; call `create_invoice_from_time` for today; re-fetch the entry with `TimeActivity.get` and print its `BillableStatus`, the invoice id and doc number; exit 0 if the status is `HasBeenBilled` or `NotBillable`, exit 1 otherwise with the message "entry still Billable: QBO did not honour LinkedTxn".
- `.env.example` lists all five variables with comments and placeholder values; no real secrets.

**Definition of Done:**

- [ ] README documents every item in the section list above and every command it quotes exists in the project (`uv run qbo-mcp authorize`, `uv run qbo-mcp serve`, `uv run python scripts/sandbox_check.py`, the `claude mcp add` line).
- [ ] `scripts/sandbox_check.py` refuses to run with `QBO_ENVIRONMENT=production` and exits 2 with a message, without contacting QBO.
- [ ] `.env.example` contains the five `QBO_*` variable names and no real values.
- [ ] Verify: `QBO_ENVIRONMENT=production QBO_CLIENT_ID=x QBO_CLIENT_SECRET=y uv run python scripts/sandbox_check.py; test $? -eq 2`

### Task 7: Intuit app, sandbox company, authorise

**Objective:** Provide the credentials and consent that only the account owner can supply, so the end-to-end check in Task 8 can run against a real sandbox company.

**Owner:** User

**User Action:** In the Intuit Developer portal create (or open) an app with the Accounting scope, add `http://localhost:8765/callback` as a sandbox redirect URI, copy the sandbox Client ID and Client Secret into `.env` as `QBO_CLIENT_ID` and `QBO_CLIENT_SECRET`, enable "Make Single-Time Activity Billable to Customer" in the sandbox company's Account and Settings → Advanced → Time tracking, then run `uv run qbo-mcp authorize` and approve access in the browser.

**Files:**

- Modify: `.env`

**Key Decisions / Notes:**

- `.env` is gitignored (Task 1) and already exists empty in the workspace.
- The token file lands at `~/.qbo_mcp/tokens-sandbox.json` unless `QBO_TOKEN_PATH` is set.

**Definition of Done:**

- [ ] `.env` contains non-empty `QBO_CLIENT_ID` and `QBO_CLIENT_SECRET` and `QBO_ENVIRONMENT=sandbox`.
- [ ] `~/.qbo_mcp/tokens-sandbox.json` exists with a non-empty `realm_id`.
- [ ] Verify: `uv run python -c "from qbo_mcp.config import Settings; from qbo_mcp.auth import TokenStore; s=Settings.load(); t=TokenStore(s.token_path).load(); print('realm', t.realm_id)"`

### Task 8: Sandbox end-to-end check, link-behaviour decision, Claude Code registration

**Objective:** Run the sandbox check against the authorised company, decide whether QBO honours the time-entry link, apply the documented fallback if it does not, and register the server with Claude Code so the tools are available in a session.

**Files:**

- Modify: `src/qbo_mcp/server.py`
- Modify: `README.md`
- Modify: `tests/test_server.py`

**Key Decisions / Notes:**

- Run `uv run python scripts/sandbox_check.py`. If the entry ends as `HasBeenBilled`, leave `MARK_NOT_BILLABLE_ON_INVOICE = False` and record in the README that linking is honoured. If it ends as `Billable`, set `MARK_NOT_BILLABLE_ON_INVOICE = True` in `server.py`, add a `test_server.py` case asserting the tool passes the flag through, re-run the sandbox check, and document in the README that invoiced entries are marked `NotBillable` because QBO ignored the link.
- If the save itself is rejected because of the line-level `LinkedTxn` (a `ValidationException` naming `LinkedTxn`), move the links to `invoice.LinkedTxn` (transaction level) in `qbo.py`, update the Task 4 payload test accordingly, and re-run.
- Register with `claude mcp add quickbooks-time -- uv run --directory /Users/trevorholmes/local-git/quickbooks_mcp qbo-mcp serve` (user scope is fine; the command uses an absolute path so cwd does not matter).

**Definition of Done:**

- [ ] `uv run python scripts/sandbox_check.py` exits 0 and prints an invoice id and doc number; the invoice is visible in the sandbox company's Sales → Invoices list with the entry's line.
- [ ] `MARK_NOT_BILLABLE_ON_INVOICE` matches the observed behaviour and the README states which path is in effect.
- [ ] Running `scripts/sandbox_check.py` a second time immediately after the first exits 0 without a "run authorize" error, confirming the rotated refresh token was persisted; the token file's `saved_at` is newer than before the first run.
- [ ] `claude mcp list` shows `quickbooks-time` as connected.
- [ ] Verify: `uv run pytest -q && claude mcp list | grep quickbooks-time`
