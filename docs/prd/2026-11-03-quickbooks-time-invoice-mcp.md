# QuickBooks Time & Invoice MCP Server

Created: 2026-11-03
Author: tholmes4005@gmail.com
Agent: Claude Code
Category: Integration
Status: Final
Research: Standard

## Problem Statement

Logging billable hours and turning them into client invoices in QuickBooks Online (QBO) is a repetitive, click-heavy chore: open the time tracker, pick the worker, pick the customer, pick the service, enter hours, then later open a new invoice, pull in the unbilled time, and save. The owner of this workspace wants to do both from a Claude Code conversation ("log 3 hours for Sam on Acme yesterday", "invoice Acme for October") without leaving the terminal.

This PRD defines a local, Python-based Model Context Protocol (MCP) server that Claude Code launches as a child process. It exposes a small set of tools for recording time against a client and generating an invoice that includes that time, backed by the QBO Accounting API. Nothing like this exists in this workspace yet; the only prior art is Intuit's official TypeScript MCP server, which the user has explicitly chosen not to use in favour of a Python implementation.

## Core User Flows

### Flow 1: One-time setup and authorisation

1. User creates an app on the Intuit Developer portal, copies the Client ID and Client Secret into the project's `.env`, and registers the local redirect URI in the app's Keys tab.
2. User creates a Python virtual environment and installs the project.
3. User runs the server's authorisation command. The server prints an Intuit consent URL and listens on a local port for the callback.
4. User opens the URL, signs in, picks the sandbox or production company, and approves access.
5. The server captures the authorisation code and realm ID from the callback, exchanges them for tokens, and stores the tokens and realm ID locally outside version control.
6. User registers the server with Claude Code once (`claude mcp add ...`). From then on the server starts automatically with each Claude Code session and refreshes its access token silently.

### Flow 2: Enter time for a worker against a client

1. User tells Claude Code, in natural language, who worked, for which client, how long, on what date, and optionally what service and a description.
2. Claude Code calls lookup tools to resolve the worker (employee or vendor), the customer, and the service item to QBO IDs. If a name is ambiguous or not found, the tool returns the candidates and Claude asks the user.
3. Claude Code calls the time-entry tool with the resolved IDs, hours and minutes (or start and end times), the date, an optional hourly rate, optional description, and whether the time is billable (default billable).
4. The server creates the TimeActivity in QBO and returns the new entry's ID, worker, customer, hours and billable status.
5. Claude Code confirms the entry to the user.

### Flow 3: Generate an invoice for a client that includes the time entries

1. User asks Claude Code to invoice a client, optionally for a date range.
2. Claude Code resolves the customer and calls the invoice tool with the customer ID and optional start and end dates.
3. The server queries QBO for every TimeActivity for that customer whose billable status is Billable (not yet billed) and whose date falls within the range.
4. If no unbilled time exists, the tool returns that fact and creates nothing.
5. Otherwise the server creates one Invoice for the customer with one line per time entry. Each line carries the service item, the quantity in hours, the rate from the time entry (falling back to the service item's price when the entry has none), and a description containing the date, worker name and the entry's own description. Each time entry is linked to the invoice so QBO marks it as billed and it no longer appears as unbilled time.
6. The tool returns the invoice ID, document number, total, and the list of time entries it included.
7. Claude Code summarises the invoice to the user.

### Flow 4: Review what has been logged

1. User asks what unbilled time exists for a client, or what was logged recently.
2. Claude Code calls the time-entry listing tool with a customer and optional date range or billable filter.
3. The tool returns the matching entries with worker, date, hours, rate, description and billable status so the user can check before invoicing.

## Scope

### In Scope

- A Python package containing a stdio MCP server that Claude Code can register and launch locally.
- Virtual-environment-based setup with pinned dependencies and a README covering Intuit app creation, environment variables, authorisation, and the `claude mcp add` registration command.
- A one-time OAuth 2.0 authorisation command with a local callback listener, local token persistence outside version control, and automatic access-token refresh during normal operation.
- Support for both QBO sandbox and production companies, selected by configuration, defaulting to sandbox.
- Tool: create a time entry for a worker (employee or vendor) against a customer, with hours and minutes or start and end times, date, service item, optional hourly rate, optional description, and billable flag.
- Tool: list time entries for a customer with optional date range and billable-status filter.
- Tool: create an invoice for a customer from its unbilled billable time entries, optionally restricted to a date range, with one line per time entry and the entries linked to the invoice.
- Lookup tools needed to drive the above from natural language: search customers, search workers (employees and vendors together), and search service items by name, returning IDs.
- Clear, actionable error messages from every tool (unknown customer, no unbilled time, token expired and re-authorisation required, QBO validation failure).
- Unit tests with the QBO client faked, plus a documented manual check against a sandbox company.

### Explicitly Out of Scope

- Sending or emailing the invoice to the client, and payment collection. The invoice is created and left for the user to review and send from QBO. Sending is a separate, irreversible action that should not be bundled into generation.
- Editing or deleting time entries and invoices after creation. Corrections happen in the QBO UI for now; adding update and delete tools is a natural follow-up once the create flows are proven.
- Creating customers, employees, vendors, or service items. These are rare setup actions and the QBO UI already handles them; the lookup tools report "not found" instead.
- Attaching expenses, estimates, or non-time charges to the invoice. The request is specifically time-based invoicing.
- Remote or HTTP transport, multi-user access, and multi-company switching. The server is a single-user local process over stdio for one QBO company at a time.
- Reports, balances, and any other read-only accounting data. Intuit's official MCP server and the Claude.ai QuickBooks connector already cover that surface.

## Technical Context

- **Runtime and packaging:** Python 3.10 or newer, installed into a project-local virtual environment managed with `uv` (already installed on this machine alongside Python 3.14). Secrets live in the existing `.env` file, which is currently empty.
- **MCP SDK:** the official `mcp` package on PyPI is at 2.3.0. Version 2 renamed the high-level `FastMCP` class to `MCPServer` and removed the old import path, so examples written for 1.x will not run unchanged. Claude Code registers a stdio server with `claude mcp add <name> -- <launch command>`, and nothing but the SDK may write to stdout.
- **QuickBooks SDKs:** Intuit's Python guidance points to the community-supported `python-quickbooks` (0.9.12 on PyPI) for API objects and `intuit-oauth` (1.2.7) for OAuth 2.0. `python-quickbooks` already models `TimeActivity`, `Invoice`, `SalesItemLine`, `Customer`, `Employee`, `Vendor`, and `Item`, with `to_ref()` helpers and a `LinkedTxn` list on `Invoice`.
- **OAuth constraints:** authorisation-code flow with the `com.intuit.quickbooks.accounting` scope. The redirect URI must be registered in the Intuit app's Keys tab; sandbox apps accept localhost redirects. The callback carries `state`, `code`, and `realmId`. Access tokens expire after one hour and refresh tokens after 100 days, after which the user must re-run the authorisation flow.
- **TimeActivity contract:** `NameOf` must be `Employee` or `Vendor` and the matching `EmployeeRef` or `VendorRef` must be set. `CustomerRef`, `ItemRef`, `TxnDate`, `Description`, `HourlyRate`, `BillableStatus` (`Billable` or `NotBillable`), and either `Hours`/`Minutes` or `StartTime`/`EndTime` are supported. `BillableStatus` may only be set when the company preference to mark time entries billable is enabled. Apps cannot set `HasBeenBilled` themselves; QBO sets it when the entry is linked to an invoice.
- **Invoice linking contract:** Intuit's reference and linked-transactions guide state that `Invoice.LinkedTxn` entries with `TxnType` `TimeActivity` can be created, read and updated via the API. Developer-forum threads report cases where the link was not reflected on the invoice line, and one Intuit support reply claimed the UI's "add billable time" linkage is not exposed. This is the main technical risk and must be confirmed against a sandbox company early; if linking fails, the fallback is to build the lines manually and flip the entries to `NotBillable` so they stop appearing as unbilled.
- **Invoice line contract:** each sales line needs an `ItemRef` pointing at a service item, a quantity, and a unit price; invoice totals and document numbers are assigned by QBO.
- **Prior art:** Intuit publishes an official QuickBooks Online MCP server in TypeScript with `create_time_activity`, `create_invoice`, and search tools. It is useful as a reference for tool naming and error conventions, but it is not Python and does not implement the sweep-unbilled-time-into-an-invoice flow.
- **Existing code:** none. The workspace contains only `create_spec.md` (the original request) and an empty `.env`.

## Research Findings

- Intuit's TimeActivity and Invoice references and the "Manage linked transactions" workflow page document `Invoice.LinkedTxn` to `TimeActivity` as create/read/update at the API level, and document that QBO automatically marks a linked entry `HasBeenBilled` (error 2170 if an app tries to set it directly). Sources: developer.intuit.com API reference for Invoice, "Manage linked transactions", and "Fix common errors".
- Intuit developer-forum threads ("Link Time Activities to Invoice"; "Programmatically set a TimeActivity's BillableStatus to HasBeenBilled") report inconsistent behaviour of that link and a support reply recommending manual lines plus setting `NotBillable`. Treated as a risk, not a fact, pending sandbox validation.
- Intuit's Python SDK page and Python OAuth client page describe `intuitlib.client.AuthClient` with `get_authorization_url`, `get_bearer_token(auth_code, realm_id=...)`, and `refresh(refresh_token=...)`, with `environment` set to `sandbox` or `production`.
- The `python-quickbooks` source confirms the `TimeActivity` fields listed above and the `Invoice` object's `LinkedTxn` list and `SalesItemLine` detail lines.
- The MCP Python SDK docs (version 2.x) show the minimal stdio server pattern and the exact Claude Code registration command using `uv run`.
- Intuit's official TypeScript MCP server (github.com/intuit/quickbooks-online-mcp-server) and the Python `mcp-quickbooks` package on PyPI exist as prior art; neither is a drop-in for this request (wrong language, or no time-entry support respectively).

## Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Worker type | Both employees and vendors, resolved by one lookup | Time in QBO must belong to an Employee or a Vendor; supporting both covers staff and contractors without two tools. User confirmed. |
| Which time goes on an invoice | Sweep all unbilled billable time for the customer, optional date range | Mirrors QBO's own "add billable time" behaviour and avoids making Claude look up IDs first. User confirmed. |
| Invoice line shape and rate | One line per time entry; rate from the entry, falling back to the service item's price | Preserves the date, worker and description detail clients expect on a time-based invoice. User confirmed. |
| Invoice is created, not sent | Create only | Sending is irreversible and client-facing; the user reviews in QBO first. |
| Authorisation mechanism | One-time command with a local callback listener; tokens persisted locally, ignored by git | Intuit requires a registered redirect URI and sandbox allows localhost; a listener avoids copy-pasting codes. Reversible assumption. |
| Environment selection | Configuration value, default sandbox | Prevents accidental writes to a live company during development. Reversible assumption. |
| Dependencies | `mcp` 2.x, `python-quickbooks`, `intuit-oauth` | These are the libraries Intuit's own Python guidance names, and the user linked those pages. |
| Lookup tools included | Search customers, workers, and service items | The two requested tools are unusable from natural language without ID resolution. |
| Linking risk handling | Validate `LinkedTxn` to `TimeActivity` in sandbox first; fallback is manual lines plus `NotBillable` | Official docs and forum reports disagree; the fallback keeps the flow working either way. |
