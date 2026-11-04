import pytest

from qbo_mcp.qbo import create_time_entry, list_time_entries
from tests.conftest import FakeQuickBooks


def _entry_row(id_, status="Billable", **extra):
    row = {
        "Id": id_,
        "TxnDate": "2026-10-15",
        "NameOf": "Employee",
        "EmployeeRef": {"value": "55", "name": "Sam Lee"},
        "CustomerRef": {"value": "42", "name": "Acme Corp"},
        "ItemRef": {"value": "9", "name": "Consulting"},
        "Hours": 1,
        "Minutes": 30,
        "HourlyRate": 120,
        "BillableStatus": status,
        "Description": "Design review",
        "SyncToken": "0",
    }
    row.update(extra)
    return row


# --- create_time_entry --------------------------------------------------------


def test_create_employee_entry_sends_expected_payload():
    qb = FakeQuickBooks()
    entry = create_time_entry(
        qb,
        worker_kind="Employee",
        worker_id="55",
        customer_id="42",
        item_id="9",
        txn_date="2026-10-15",
        hours=1,
        minutes=30,
        hourly_rate=120,
        description="Design review",
    )
    name, body = qb.created[0]
    assert name == "TimeActivity"
    assert body["NameOf"] == "Employee"
    assert body["EmployeeRef"]["value"] == "55"
    assert "VendorRef" not in body
    assert body["CustomerRef"]["value"] == "42"
    assert body["ItemRef"]["value"] == "9"
    assert body["TxnDate"] == "2026-10-15"
    assert body["Hours"] == 1 and body["Minutes"] == 30
    assert body["HourlyRate"] == 120
    assert body["BillableStatus"] == "Billable"
    assert body["Description"] == "Design review"
    assert "StartTime" not in body
    assert entry.id == "1000"
    assert entry.worker_kind == "Employee" and entry.worker_id == "55"
    assert entry.hours == 1 and entry.minutes == 30
    assert entry.billable_status == "Billable"


def test_create_vendor_entry_not_billable():
    qb = FakeQuickBooks()
    create_time_entry(
        qb, worker_kind="Vendor", worker_id="77", customer_id="42", item_id="9", txn_date="2026-10-15", hours=2, billable=False
    )
    body = qb.created[0][1]
    assert body["NameOf"] == "Vendor"
    assert body["VendorRef"]["value"] == "77"
    assert "EmployeeRef" not in body
    assert body["BillableStatus"] == "NotBillable"
    assert body["Hours"] == 2 and body["Minutes"] == 0


def test_create_entry_with_start_and_end_times():
    qb = FakeQuickBooks()
    entry = create_time_entry(
        qb,
        worker_kind="Employee",
        worker_id="55",
        customer_id="42",
        item_id="9",
        txn_date="2026-10-15",
        start_time="2026-10-15T09:00:00-05:00",
        end_time="2026-10-15T11:15:00-05:00",
    )
    body = qb.created[0][1]
    assert body["StartTime"] == "2026-10-15T09:00:00-05:00"
    assert body["EndTime"] == "2026-10-15T11:15:00-05:00"
    assert "Hours" not in body and "Minutes" not in body
    # Duration is derived from the timestamps when QBO does not echo Hours/Minutes.
    assert (entry.hours, entry.minutes) == (2, 15)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"hours": 1, "start_time": "2026-10-15T09:00:00-05:00", "end_time": "2026-10-15T10:00:00-05:00"}, "either"),
        ({}, "either"),
        ({"hours": 0, "minutes": 0}, "greater than zero"),
        ({"hours": -1}, "greater than zero"),
        ({"hours": 1, "worker_kind": "Contractor"}, "Employee"),
        ({"hours": 1, "txn_date": "15/10/2026"}, "YYYY-MM-DD"),
    ],
)
def test_create_entry_rejects_invalid_input(kwargs, match):
    qb = FakeQuickBooks()
    args = dict(worker_kind="Employee", worker_id="55", customer_id="42", item_id="9", txn_date="2026-10-15")
    args.update(kwargs)
    with pytest.raises(ValueError, match=match):
        create_time_entry(qb, **args)
    assert qb.created == []


# --- list_time_entries --------------------------------------------------------


def test_list_builds_where_clause_and_maps_rows():
    qb = FakeQuickBooks(query_handler=lambda s: [_entry_row("1"), _entry_row("2", "HasBeenBilled")])
    entries = list_time_entries(qb, customer_id="42", start_date="2026-10-01", end_date="2026-10-31")
    assert "WHERE CustomerRef = '42' AND TxnDate >= '2026-10-01' AND TxnDate <= '2026-10-31'" in qb.queries[0]
    assert "ORDERBY TxnDate" in qb.queries[0]
    assert "MAXRESULTS 1000" in qb.queries[0]
    assert [e.id for e in entries] == ["1", "2"]
    first = entries[0]
    assert first.worker_name == "Sam Lee" and first.customer_name == "Acme Corp" and first.item_name == "Consulting"
    assert first.hourly_rate == 120.0 and first.description == "Design review"


def test_list_without_dates_only_filters_customer():
    qb = FakeQuickBooks(query_handler=lambda s: [])
    assert list_time_entries(qb, customer_id="42") == []
    assert "WHERE CustomerRef = '42' ORDERBY" in qb.queries[0]


def test_list_billable_only_drops_billed_and_not_billable():
    rows = [_entry_row("1", "Billable"), _entry_row("2", "HasBeenBilled"), _entry_row("3", "NotBillable")]
    qb = FakeQuickBooks(query_handler=lambda s: rows)
    entries = list_time_entries(qb, customer_id="42", billable_only=True)
    assert [e.id for e in entries] == ["1"]


def test_list_pages_when_a_full_page_is_returned():
    pages = [[_entry_row(str(i)) for i in range(1000)], [_entry_row("x")]]

    def handler(select):
        return pages.pop(0)

    qb = FakeQuickBooks(query_handler=handler)
    entries = list_time_entries(qb, customer_id="42")
    assert len(entries) == 1001
    assert "STARTPOSITION 1 " in qb.queries[0]
    assert "STARTPOSITION 1001 " in qb.queries[1]


def test_list_derives_duration_from_times_when_hours_missing():
    row = _entry_row("1", Hours=None, Minutes=None, StartTime="2026-10-15T09:00:00-05:00", EndTime="2026-10-15T12:30:00-05:00", BreakHours=0, BreakMinutes=30)
    row = {k: v for k, v in row.items() if v is not None}
    qb = FakeQuickBooks(query_handler=lambda s: [row])
    entry = list_time_entries(qb, customer_id="42")[0]
    assert (entry.hours, entry.minutes) == (3, 0)
