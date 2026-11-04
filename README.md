# qbo-mcp: QuickBooks time & invoice MCP server for Claude Code

A local [Model Context Protocol](https://modelcontextprotocol.io) server, written in Python, that lets Claude Code log time for an employee or vendor against a QuickBooks Online customer and then create an invoice from that customer's unbilled time.

Tools: `search_customers`, `search_workers`, `search_service_items`, `create_time_entry`, `list_time_entries`, `create_invoice_from_time`.

## Prerequisites

- Python 3.10 or newer and [`uv`](https://docs.astral.sh/uv/).
- An [Intuit Developer](https://developer.intuit.com) account. Free accounts come with a sandbox QuickBooks Online company.
- Claude Code (for the `claude mcp add` step).

## 1. Create the Intuit app

1. Sign in to the Intuit Developer portal and create an app (QuickBooks Online and Payments platform) with the **Accounting** scope.
2. Open the app's **Keys & credentials** page. Under **Development** (sandbox) copy the **Client ID** and **Client Secret**.
3. On the same page add `http://localhost:8765/callback` to the **Redirect URIs** list and save.
4. In the sandbox company itself, enable billable time: open the company at [sandbox](https://app.sandbox.qbo.intuit.com), go to **Settings (gear) → Account and settings → Advanced → Time tracking** and turn on **Make Single-Time Activity Billable to Customer**. Without this, QuickBooks rejects billable time entries.

## 2. Install

```bash
uv sync
```

This creates `.venv/` with pinned dependencies (`mcp` 2.x, `python-quickbooks`, `intuit-oauth`).

## 3. Configure

```bash
cp .env.example .env
```

Fill in `QBO_CLIENT_ID` and `QBO_CLIENT_SECRET`. The other variables have sensible defaults:

| Variable | Default | Purpose |
|----------|---------|---------|
| `QBO_ENVIRONMENT` | `sandbox` | `sandbox` or `production`. Tokens are stored per environment. |
| `QBO_REDIRECT_URI` | `http://localhost:8765/callback` | Must match a redirect URI registered on the app. |
| `QBO_TOKEN_PATH` | `~/.qbo_mcp/tokens-<environment>.json` | Where the OAuth tokens are saved (mode 0600). |

`.env` and token files are gitignored. Process environment variables override `.env`.

## 4. Authorise

```bash
uv run qbo-mcp authorize
```

The command prints an Intuit consent URL, opens it in your browser, and listens on the redirect port. Sign in, pick the sandbox company, and approve. The tokens and company (realm) ID are written to the token file and the command exits. Use `--no-browser` to only print the URL.

Access tokens last one hour and are refreshed automatically. Refresh tokens last 100 days; after that, run `authorize` again.

## 5. Register with Claude Code

```bash
claude mcp add quickbooks-time -- uv run --directory /Users/trevorholmes/local-git/quickbooks_mcp qbo-mcp serve
```

Replace the directory with the absolute path of this checkout. Check it with `claude mcp list` (the server should show as connected) and `/mcp` inside a session to see the six tools.

## Tools

| Tool | Arguments | Returns |
|------|-----------|---------|
| `search_customers` | `query` (partial name, optional) | Customers with `id`, `display_name`, `company_name`, `active` |
| `search_workers` | `query` (partial name, optional) | Employees and vendors with `id`, `display_name`, `kind` |
| `search_service_items` | `query` (partial name, optional) | Service items with `id`, `name`, `unit_price` |
| `create_time_entry` | `worker_kind`, `worker_id`, `customer_id`, `item_id`, `txn_date`, then either `hours`/`minutes` or `start_time`/`end_time`; optional `hourly_rate`, `description`, `billable` (default true) | The created time entry |
| `list_time_entries` | `customer_id`; optional `start_date`, `end_date`, `billable_only` | Time entries with worker, date, duration, rate, billable status |
| `create_invoice_from_time` | `customer_id`; optional `start_date`, `end_date` | `created`, `message`, and the invoice (ID, document number, total, one line per time entry) |

Dates are `YYYY-MM-DD`; timestamps are ISO-8601 with an offset such as `2026-11-03T09:00:00-05:00`.

`create_invoice_from_time` sweeps every time entry for the customer that is **Billable** and not yet invoiced (optionally within the date range), creates one invoice with one line per entry (description `"<date> <worker>: <entry description>"`, quantity in hours, rate from the entry or else the service item's price), and links each entry to its line so QuickBooks marks it **HasBeenBilled**. If any entry has no service item or no resolvable rate, nothing is created and the tool names the entries to fix. The invoice is **not** sent; review and send it from QuickBooks.

## Example conversation

> **You:** Log 2 hours for Sam Lee on Acme Corp yesterday, consulting, "API design review".
>
> Claude calls `search_workers("Sam Lee")`, `search_customers("Acme")`, `search_service_items("consulting")`, then `create_time_entry(...)` and confirms the entry.
>
> **You:** Invoice Acme for October.
>
> Claude calls `search_customers("Acme")` then `create_invoice_from_time(customer_id, start_date="2026-10-01", end_date="2026-10-31")` and reports the invoice number and total.

## Sandbox check

With `.env` pointing at the sandbox and `authorize` done:

```bash
uv run python scripts/sandbox_check.py
```

It creates a 1h30m billable entry for the first customer, worker and service item it finds (override with `--customer`, `--worker`, `--item`), invoices today's unbilled time, and re-reads the entry. Exit code 0 means the entry no longer counts as unbilled; 1 means QuickBooks ignored the link; 2 means it refused to run (not a sandbox, not authorised, or nothing to pick). The script refuses to run against production.

Link behaviour observed in the sandbox is recorded below once the check has been run.

## Production keys and the policy pages

Intuit unlocks an app's **Production** keys only after the app has a host domain, launch URL, disconnect URL, connect URL, privacy policy URL and terms-of-service URL, all served over HTTPS. `docs/site/` holds a small static site that satisfies all six:

| Intuit field | Value (replace the domain) |
|--------------|----------------------------|
| Host domain | `yourname.github.io` |
| Launch URL and Connect/Reconnect URL | `https://yourname.github.io/quickbooks_mcp/` |
| Disconnect URL | `https://yourname.github.io/quickbooks_mcp/disconnect.html` |
| Privacy policy URL | `https://yourname.github.io/quickbooks_mcp/privacy.html` |
| Terms of service URL | `https://yourname.github.io/quickbooks_mcp/terms.html` |

To publish: push the repository to GitHub (public, unless the account's plan allows Pages on private repositories) and under **Settings → Pages** set the source to **GitHub Actions**. The workflow in `.github/workflows/pages.yml` uploads only `docs/site/` (never the planning documents) on each push to `main`. Any other HTTPS host works too; only the URLs change.

These pages are informational. The OAuth redirect URI is a separate setting on the Keys & credentials page, and the server never reads the pages.

## Running the tests

```bash
uv run pytest -q
```

The tests use fakes for the QuickBooks and OAuth clients and never touch the network.

## Troubleshooting

- **"QuickBooks authorisation is missing or expired"**: run `uv run qbo-mcp authorize`. Refresh tokens expire after 100 days of inactivity or when revoked.
- **"Cannot listen on localhost:8765"**: another program holds the port. Stop it, or set `QBO_REDIRECT_URI` to a free port and register that URI on the Intuit app.
- **"Callback state does not match"**: the browser hit the callback with a stale link. Re-run `authorize` and use the newly printed URL.
- **QuickBooks rejects `BillableStatus`**: enable **Make Single-Time Activity Billable to Customer** in the company settings (step 1.4).
- **Switching to production**: in the Intuit app copy the **Production** keys, set `QBO_ENVIRONMENT=production` and the production keys in `.env`, register the redirect URI under production keys (Intuit may require HTTPS there), then run `authorize` again. Tokens are stored separately per environment.
- **Claude Code shows the server as failed**: run `uv run qbo-mcp serve` manually; it should start silently and wait on stdin. Any error printed to stderr is the cause. Nothing may write to stdout while serving.
