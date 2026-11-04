"""QuickBooks Online operations used by the MCP tools.

Every function takes a ``quickbooks.QuickBooks`` client as its first argument and
returns plain dataclasses, so the MCP layer never touches python-quickbooks objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from quickbooks.objects.base import LinkedTxn, Ref
from quickbooks.objects.customer import Customer
from quickbooks.objects.detailline import SalesItemLine, SalesItemLineDetail
from quickbooks.objects.employee import Employee
from quickbooks.objects.invoice import Invoice
from quickbooks.objects.item import Item
from quickbooks.objects.timeactivity import TimeActivity
from quickbooks.objects.vendor import Vendor

SEARCH_MAX_RESULTS = 25
QUERY_PAGE_SIZE = 1000  # QBO's hard cap per query
WORKER_KINDS = ("Employee", "Vendor")
BILLABLE_STATUSES = ("Billable", "NotBillable", "HasBeenBilled")


# --- Result types -------------------------------------------------------------


@dataclass
class CustomerMatch:
    id: str
    display_name: str
    company_name: str | None
    active: bool


@dataclass
class WorkerMatch:
    id: str
    display_name: str
    kind: str  # "Employee" or "Vendor"


@dataclass
class ItemMatch:
    id: str
    name: str
    unit_price: float | None
    type: str


@dataclass
class InvoiceLineResult:
    time_entry_id: str
    description: str
    hours: float
    rate: float
    amount: float


@dataclass
class InvoiceResult:
    invoice_id: str
    doc_number: str | None
    txn_date: str | None
    total: float | None
    customer_name: str | None
    lines: list[InvoiceLineResult]


@dataclass
class NoUnbilledTime:
    """Returned (not raised) when the sweep finds nothing to invoice."""

    customer_id: str
    start_date: str | None
    end_date: str | None


class InvoiceBuildError(ValueError):
    """Some time entries cannot be billed as-is; nothing was created."""


@dataclass
class TimeEntry:
    id: str
    txn_date: str
    worker_kind: str
    worker_id: str
    worker_name: str | None
    customer_id: str | None
    customer_name: str | None
    item_id: str | None
    item_name: str | None
    hours: int
    minutes: int
    hourly_rate: float | None
    billable_status: str | None
    description: str | None


# --- Helpers ------------------------------------------------------------------


def _escape(value: str) -> str:
    return value.replace("'", "\\'")


def _ref(entity_type: str, value: str) -> Ref:
    ref = Ref()
    ref.type = entity_type
    ref.value = str(value)
    return ref


def _ref_value(ref: Any) -> str | None:
    return None if ref is None else (str(ref.value) if ref.value is not None else None)


def _ref_name(ref: Any) -> str | None:
    return None if ref is None else ref.name


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _parse_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a date in YYYY-MM-DD format; got {value!r}.") from None


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an ISO-8601 timestamp such as 2026-11-03T09:00:00-05:00; got {value!r}.") from None


def _duration(activity: TimeActivity) -> tuple[int, int]:
    """Return (hours, minutes), deriving from StartTime/EndTime when QBO omits Hours."""
    hours = activity.Hours
    minutes = activity.Minutes
    if hours is not None or minutes is not None:
        return int(hours or 0), int(minutes or 0)
    if activity.StartTime and activity.EndTime:
        start = _parse_datetime(str(activity.StartTime), "StartTime")
        end = _parse_datetime(str(activity.EndTime), "EndTime")
        total_minutes = int((end - start).total_seconds() // 60)
        total_minutes -= int(activity.BreakHours or 0) * 60 + int(activity.BreakMinutes or 0)
        total_minutes = max(total_minutes, 0)
        return total_minutes // 60, total_minutes % 60
    return 0, 0


def _to_time_entry(activity: TimeActivity) -> TimeEntry:
    kind = activity.NameOf or ("Vendor" if activity.VendorRef is not None else "Employee")
    worker_ref = activity.VendorRef if kind == "Vendor" else activity.EmployeeRef
    hours, minutes = _duration(activity)
    return TimeEntry(
        id=str(activity.Id),
        txn_date=str(activity.TxnDate),
        worker_kind=kind,
        worker_id=_ref_value(worker_ref) or "",
        worker_name=_ref_name(worker_ref),
        customer_id=_ref_value(activity.CustomerRef),
        customer_name=_ref_name(activity.CustomerRef),
        item_id=_ref_value(activity.ItemRef),
        item_name=_ref_name(activity.ItemRef),
        hours=hours,
        minutes=minutes,
        hourly_rate=_to_float(activity.HourlyRate),
        billable_status=activity.BillableStatus,
        description=activity.Description,
    )


def _name_search_clause(field: str, query: str) -> str:
    clause = "Active = true"
    query = (query or "").strip()
    if query:
        clause += f" AND {field} LIKE '%{_escape(query)}%'"
    return clause


# --- Lookups ------------------------------------------------------------------


def search_customers(qb: Any, query: str = "") -> list[CustomerMatch]:
    rows = Customer.where(_name_search_clause("DisplayName", query), order_by="DisplayName", max_results=SEARCH_MAX_RESULTS, qb=qb)
    return [
        CustomerMatch(id=str(c.Id), display_name=c.DisplayName, company_name=c.CompanyName or None, active=bool(c.Active))
        for c in rows
    ]


def search_workers(qb: Any, query: str = "") -> list[WorkerMatch]:
    clause = _name_search_clause("DisplayName", query)
    employees = Employee.where(clause, order_by="DisplayName", max_results=SEARCH_MAX_RESULTS, qb=qb)
    vendors = Vendor.where(clause, order_by="DisplayName", max_results=SEARCH_MAX_RESULTS, qb=qb)
    matches = [WorkerMatch(id=str(e.Id), display_name=e.DisplayName, kind="Employee") for e in employees]
    matches += [WorkerMatch(id=str(v.Id), display_name=v.DisplayName, kind="Vendor") for v in vendors]
    return sorted(matches, key=lambda m: (m.display_name or "").lower())


def search_service_items(qb: Any, query: str = "") -> list[ItemMatch]:
    clause = "Active = true AND Type = 'Service'"
    query = (query or "").strip()
    if query:
        clause += f" AND Name LIKE '%{_escape(query)}%'"
    rows = Item.where(clause, order_by="Name", max_results=SEARCH_MAX_RESULTS, qb=qb)
    return [ItemMatch(id=str(i.Id), name=i.Name, unit_price=_to_float(i.UnitPrice), type=i.Type) for i in rows]


# --- Time entries -------------------------------------------------------------


def create_time_entry(
    qb: Any,
    *,
    worker_kind: str,
    worker_id: str,
    customer_id: str,
    item_id: str,
    txn_date: str,
    hours: int | None = None,
    minutes: int | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    hourly_rate: float | None = None,
    description: str | None = None,
    billable: bool = True,
) -> TimeEntry:
    if worker_kind not in WORKER_KINDS:
        raise ValueError(f"worker_kind must be 'Employee' or 'Vendor'; got {worker_kind!r}.")
    txn_date = _parse_date(txn_date, "txn_date")

    has_duration = hours is not None or minutes is not None
    has_times = start_time is not None or end_time is not None
    if has_duration == has_times:
        raise ValueError("Provide either hours/minutes or start_time/end_time, not both and not neither.")

    activity = TimeActivity()
    activity.NameOf = worker_kind
    if worker_kind == "Employee":
        activity.EmployeeRef = _ref("Employee", worker_id)
    else:
        activity.VendorRef = _ref("Vendor", worker_id)
    activity.CustomerRef = _ref("Customer", customer_id)
    activity.ItemRef = _ref("Item", item_id)
    activity.TxnDate = txn_date
    activity.BillableStatus = "Billable" if billable else "NotBillable"
    activity.Taxable = None  # let QBO apply the company default
    if hourly_rate is not None:
        activity.HourlyRate = hourly_rate
    if description:
        activity.Description = description

    if has_duration:
        total = int(hours or 0) * 60 + int(minutes or 0)
        if int(hours or 0) < 0 or int(minutes or 0) < 0 or total <= 0:
            raise ValueError("Duration must be greater than zero (hours and minutes cannot be negative).")
        activity.Hours = total // 60
        activity.Minutes = total % 60
    else:
        if not start_time or not end_time:
            raise ValueError("Both start_time and end_time are required when logging by time range.")
        start = _parse_datetime(start_time, "start_time")
        end = _parse_datetime(end_time, "end_time")
        if end <= start:
            raise ValueError("end_time must be after start_time.")
        activity.StartTime = start_time
        activity.EndTime = end_time

    saved = activity.save(qb=qb)
    return _to_time_entry(saved)


def list_time_entries(
    qb: Any,
    *,
    customer_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    billable_only: bool = False,
) -> list[TimeEntry]:
    clause = f"CustomerRef = '{_escape(str(customer_id))}'"
    if start_date:
        clause += f" AND TxnDate >= '{_parse_date(start_date, 'start_date')}'"
    if end_date:
        clause += f" AND TxnDate <= '{_parse_date(end_date, 'end_date')}'"

    entries: list[TimeEntry] = []
    position = 1
    while True:
        page = TimeActivity.where(clause, order_by="TxnDate", start_position=position, max_results=QUERY_PAGE_SIZE, qb=qb)
        entries.extend(_to_time_entry(a) for a in page)
        if len(page) < QUERY_PAGE_SIZE:
            break
        position += QUERY_PAGE_SIZE

    if billable_only:
        entries = [e for e in entries if e.billable_status == "Billable"]
    return entries


def get_time_entry(qb: Any, entry_id: str) -> TimeEntry:
    return _to_time_entry(TimeActivity.get(entry_id, qb=qb))


# --- Invoicing ----------------------------------------------------------------


def _line_description(entry: TimeEntry) -> str:
    text = f"{entry.txn_date} {entry.worker_name or entry.worker_id}"
    if entry.description:
        text += f": {entry.description}"
    return text


def _resolve_rate(qb: Any, entry: TimeEntry, item_price_cache: dict[str, float | None]) -> float | None:
    if entry.hourly_rate:
        return float(entry.hourly_rate)
    if entry.item_id is None:
        return None
    if entry.item_id not in item_price_cache:
        item_price_cache[entry.item_id] = _to_float(Item.get(entry.item_id, qb=qb).UnitPrice)
    return item_price_cache[entry.item_id]


def create_invoice_from_time(
    qb: Any,
    *,
    customer_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    mark_not_billable_on_invoice: bool = False,
) -> InvoiceResult | NoUnbilledTime:
    """Create one invoice holding every unbilled billable time entry for the customer.

    Each entry becomes its own line linked back to the TimeActivity so QBO marks it
    ``HasBeenBilled``. With ``mark_not_billable_on_invoice`` the entries are instead
    updated to ``NotBillable`` after the invoice is saved (fallback for companies
    where QBO ignores the link).
    """
    entries = list_time_entries(qb, customer_id=customer_id, start_date=start_date, end_date=end_date, billable_only=True)
    if not entries:
        return NoUnbilledTime(customer_id=str(customer_id), start_date=start_date, end_date=end_date)

    price_cache: dict[str, float | None] = {}
    problems: list[str] = []
    planned: list[tuple[TimeEntry, float, float]] = []
    for entry in entries:
        if entry.item_id is None:
            problems.append(f"time entry {entry.id} ({entry.txn_date}) has no service item")
            continue
        rate = _resolve_rate(qb, entry, price_cache)
        if not rate:
            problems.append(f"time entry {entry.id} ({entry.txn_date}) has no hourly rate and its service item has no price")
            continue
        hours = round(entry.hours + entry.minutes / 60, 2)
        planned.append((entry, hours, rate))
    if problems:
        raise InvoiceBuildError(
            "Cannot build the invoice until these entries are fixed in QuickBooks (set a rate on the entry "
            "or a price on the service item): " + "; ".join(problems)
        )

    invoice = Invoice()
    invoice.CustomerRef = _ref("Customer", customer_id)
    line_results: list[InvoiceLineResult] = []
    for number, (entry, hours, rate) in enumerate(planned, start=1):
        amount = round(hours * rate, 2)
        detail = SalesItemLineDetail()
        detail.ItemRef = _ref("Item", entry.item_id or "")
        detail.Qty = hours
        detail.UnitPrice = rate
        detail.ServiceDate = entry.txn_date
        detail.TaxInclusiveAmt = None
        line = SalesItemLine()
        line.LineNum = number
        line.Description = _line_description(entry)
        line.Amount = amount
        line.SalesItemLineDetail = detail
        link = LinkedTxn()
        link.TxnId = entry.id
        link.TxnType = "TimeActivity"
        link.TxnLineId = None  # omitted from the payload; QBO rejects a line id for time links
        line.LinkedTxn = [link]
        invoice.Line.append(line)
        line_results.append(InvoiceLineResult(time_entry_id=entry.id, description=line.Description, hours=hours, rate=rate, amount=amount))

    saved = invoice.save(qb=qb)

    if mark_not_billable_on_invoice:
        for entry, _, _ in planned:
            activity = TimeActivity.get(entry.id, qb=qb)
            activity.BillableStatus = "NotBillable"
            activity.save(qb=qb)

    customer_name = _ref_name(saved.CustomerRef) or (planned[0][0].customer_name if planned else None)
    return InvoiceResult(
        invoice_id=str(saved.Id),
        doc_number=str(saved.DocNumber) if saved.DocNumber else None,
        txn_date=str(saved.TxnDate) if saved.TxnDate else None,
        total=_to_float(saved.TotalAmt) if saved.TotalAmt not in (None, "") else round(sum(l.amount for l in line_results), 2),
        customer_name=customer_name,
        lines=line_results,
    )
