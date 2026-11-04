"""MCP server exposing QuickBooks time-entry and invoicing tools to Claude Code.

All logging goes to stderr: stdout belongs to the MCP stdio transport.
"""

from __future__ import annotations

import functools
import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from intuitlib.exceptions import AuthClientError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from quickbooks.exceptions import AuthorizationException, ObjectNotFoundException, QuickbooksException, ValidationException
from typing_extensions import Annotated

from . import qbo
from .auth import NotAuthorizedError, TokenStore, build_quickbooks_client
from .config import ConfigError, Settings

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Flipped to True by the sandbox check (plan Task 8) if QBO ignores LinkedTxn on invoice lines,
# so invoiced entries are updated to NotBillable and stop appearing as unbilled time.
MARK_NOT_BILLABLE_ON_INVOICE = False

AUTHORIZE_HINT = "Run `uv run qbo-mcp authorize` in the project directory and retry."

mcp = MCPServer(
    "quickbooks-time",
    instructions=(
        "Tools for logging time and invoicing clients in QuickBooks Online. "
        "Resolve names to IDs with the search_* tools first, then call create_time_entry or "
        "create_invoice_from_time with those IDs. Dates are YYYY-MM-DD."
    ),
)

_client: Any = None


def get_client() -> Any:
    """Build the QuickBooks client once per server process."""
    global _client
    if _client is None:
        settings = Settings.load()
        _client = build_quickbooks_client(settings, TokenStore(settings.token_path))
    return _client


F = TypeVar("F", bound=Callable[..., Any])


def _maps_qbo_errors(fn: F) -> F:
    """Translate configuration, auth and QuickBooks failures into actionable ToolErrors."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except ConfigError as exc:
            raise ToolError(f"QuickBooks MCP server is not configured: {exc}") from exc
        except (NotAuthorizedError, AuthClientError, AuthorizationException) as exc:
            raise ToolError(f"QuickBooks authorisation is missing or expired ({exc}). {AUTHORIZE_HINT}") from exc
        except ObjectNotFoundException as exc:
            raise ToolError(f"Not found in QuickBooks: {exc.message} {exc.detail}".strip()) from exc
        except ValidationException as exc:
            raise ToolError(f"QuickBooks rejected the request: {exc.message} {exc.detail}".strip()) from exc
        except QuickbooksException as exc:
            raise ToolError(f"QuickBooks error {exc.error_code}: {exc.message} {exc.detail}".strip()) from exc
        except (ValueError, qbo.InvoiceBuildError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


@dataclass
class InvoiceOutcome:
    created: bool
    message: str
    invoice: qbo.InvoiceResult | None


# --- Tools --------------------------------------------------------------------


@mcp.tool()
@_maps_qbo_errors
def search_customers(
    query: Annotated[str, Field(description="Partial customer display name, case-insensitive. Empty lists the first 25 active customers.")] = "",
) -> list[qbo.CustomerMatch]:
    """Find QuickBooks customers (clients) by name and return their IDs."""
    return qbo.search_customers(get_client(), query)


@mcp.tool()
@_maps_qbo_errors
def search_workers(
    query: Annotated[str, Field(description="Partial worker display name. Searches employees and vendors (contractors).")] = "",
) -> list[qbo.WorkerMatch]:
    """Find workers whose time can be logged: employees and vendors. Returns each match's ID and kind."""
    return qbo.search_workers(get_client(), query)


@mcp.tool()
@_maps_qbo_errors
def search_service_items(
    query: Annotated[str, Field(description="Partial service item name. Empty lists the first 25 active service items.")] = "",
) -> list[qbo.ItemMatch]:
    """Find service items (what the time is billed as) and their default prices."""
    return qbo.search_service_items(get_client(), query)


@mcp.tool()
@_maps_qbo_errors
def create_time_entry(
    worker_kind: Annotated[str, Field(description="'Employee' or 'Vendor', as returned by search_workers.")],
    worker_id: Annotated[str, Field(description="Worker ID from search_workers.")],
    customer_id: Annotated[str, Field(description="Customer ID from search_customers.")],
    item_id: Annotated[str, Field(description="Service item ID from search_service_items.")],
    txn_date: Annotated[str, Field(description="Date the work was done, YYYY-MM-DD.")],
    hours: Annotated[int | None, Field(description="Whole hours worked (use with minutes). Omit when giving start_time/end_time.")] = None,
    minutes: Annotated[int | None, Field(description="Additional minutes worked (0-59).")] = None,
    start_time: Annotated[str | None, Field(description="ISO-8601 start timestamp, e.g. 2026-11-03T09:00:00-05:00.")] = None,
    end_time: Annotated[str | None, Field(description="ISO-8601 end timestamp.")] = None,
    hourly_rate: Annotated[float | None, Field(description="Billing rate per hour. Omit to bill at the service item's price.")] = None,
    description: Annotated[str | None, Field(description="What was done; appears on the invoice line.")] = None,
    billable: Annotated[bool, Field(description="Whether the time should be invoiced to the customer.")] = True,
) -> qbo.TimeEntry:
    """Log time worked by an employee or vendor against a customer. Give hours/minutes OR start_time/end_time."""
    return qbo.create_time_entry(
        get_client(),
        worker_kind=worker_kind,
        worker_id=worker_id,
        customer_id=customer_id,
        item_id=item_id,
        txn_date=txn_date,
        hours=hours,
        minutes=minutes,
        start_time=start_time,
        end_time=end_time,
        hourly_rate=hourly_rate,
        description=description,
        billable=billable,
    )


@mcp.tool()
@_maps_qbo_errors
def list_time_entries(
    customer_id: Annotated[str, Field(description="Customer ID from search_customers.")],
    start_date: Annotated[str | None, Field(description="Earliest date to include, YYYY-MM-DD.")] = None,
    end_date: Annotated[str | None, Field(description="Latest date to include, YYYY-MM-DD.")] = None,
    billable_only: Annotated[bool, Field(description="True returns only entries that are billable and not yet invoiced.")] = False,
) -> list[qbo.TimeEntry]:
    """List time entries logged against a customer, optionally within a date range or only unbilled ones."""
    return qbo.list_time_entries(get_client(), customer_id=customer_id, start_date=start_date, end_date=end_date, billable_only=billable_only)


@mcp.tool()
@_maps_qbo_errors
def create_invoice_from_time(
    customer_id: Annotated[str, Field(description="Customer ID from search_customers.")],
    start_date: Annotated[str | None, Field(description="Only include time on or after this date, YYYY-MM-DD.")] = None,
    end_date: Annotated[str | None, Field(description="Only include time on or before this date, YYYY-MM-DD.")] = None,
) -> InvoiceOutcome:
    """Create a QuickBooks invoice from the customer's unbilled billable time, one line per time entry.

    The invoice is created but not sent; review and send it from QuickBooks. Returns created=False with
    a message when there is no unbilled time.
    """
    result = qbo.create_invoice_from_time(
        get_client(),
        customer_id=customer_id,
        start_date=start_date,
        end_date=end_date,
        mark_not_billable_on_invoice=MARK_NOT_BILLABLE_ON_INVOICE,
    )
    if isinstance(result, qbo.NoUnbilledTime):
        window = ""
        if start_date or end_date:
            window = f" between {start_date or 'the beginning'} and {end_date or 'today'}"
        return InvoiceOutcome(created=False, message=f"No unbilled billable time for customer {customer_id}{window}; nothing was created.", invoice=None)
    return InvoiceOutcome(
        created=True,
        message=f"Created invoice {result.doc_number or result.invoice_id} for {result.customer_name or customer_id} with {len(result.lines)} line(s), total {result.total}.",
        invoice=result,
    )


def serve() -> None:
    """Run the server over stdio (what Claude Code launches)."""
    log.info("Starting quickbooks-time MCP server over stdio")
    mcp.run()
