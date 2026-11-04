import pytest

from qbo_mcp.qbo import InvoiceBuildError, InvoiceResult, NoUnbilledTime, create_invoice_from_time
from tests.conftest import FakeQuickBooks


def _entry(id_, status="Billable", **extra):
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
        "SyncToken": "3",
    }
    row.update(extra)
    return {k: v for k, v in row.items() if v is not None}


def _qb(rows):
    qb = FakeQuickBooks(query_handler=lambda s: rows)
    qb.objects[("Item", "9")] = {"Id": "9", "Name": "Consulting", "Type": "Service", "UnitPrice": 95}
    for row in rows:
        qb.objects[("TimeActivity", row["Id"])] = row
    return qb


def test_invoice_lines_rates_and_links():
    rows = [
        _entry("1"),
        _entry("2", Hours=2, Minutes=0, HourlyRate=None, Description=None, TxnDate="2026-10-16"),
    ]
    qb = _qb(rows)
    result = create_invoice_from_time(qb, customer_id="42")

    assert isinstance(result, InvoiceResult)
    name, body = qb.created[0]
    assert name == "Invoice"
    assert body["CustomerRef"]["value"] == "42"
    lines = body["Line"]
    assert len(lines) == 2
    assert [l["DetailType"] for l in lines] == ["SalesItemLineDetail", "SalesItemLineDetail"]
    assert [l["SalesItemLineDetail"]["Qty"] for l in lines] == [1.5, 2.0]
    assert [l["SalesItemLineDetail"]["UnitPrice"] for l in lines] == [120.0, 95.0]
    assert [l["Amount"] for l in lines] == [180.0, 190.0]
    assert [l["SalesItemLineDetail"]["ItemRef"]["value"] for l in lines] == ["9", "9"]
    assert lines[0]["Description"] == "2026-10-15 Sam Lee: Design review"
    assert lines[1]["Description"] == "2026-10-16 Sam Lee"
    assert lines[0]["LinkedTxn"] == [{"TxnId": "1", "TxnType": "TimeActivity"}]
    assert lines[1]["LinkedTxn"] == [{"TxnId": "2", "TxnType": "TimeActivity"}]
    assert "TxnLineId" not in lines[0]["LinkedTxn"][0]

    assert result.invoice_id == "1000"
    assert result.doc_number == "1042"
    assert result.total == 370.0
    assert result.customer_name == "Acme Corp"
    assert [(l.time_entry_id, l.hours, l.rate, l.amount) for l in result.lines] == [("1", 1.5, 120.0, 180.0), ("2", 2.0, 95.0, 190.0)]
    # Item price looked up once, not per line.
    assert qb.updated == []


def test_no_unbilled_time_creates_nothing():
    qb = _qb([_entry("1", "HasBeenBilled"), _entry("2", "NotBillable")])
    result = create_invoice_from_time(qb, customer_id="42", start_date="2026-10-01", end_date="2026-10-31")
    assert isinstance(result, NoUnbilledTime)
    assert result.customer_id == "42" and result.start_date == "2026-10-01" and result.end_date == "2026-10-31"
    assert qb.created == []
    assert "TxnDate >= '2026-10-01' AND TxnDate <= '2026-10-31'" in qb.queries[0]


def test_entry_without_item_or_rate_blocks_whole_invoice():
    rows = [_entry("1"), _entry("2", ItemRef=None), _entry("3", HourlyRate=0, ItemRef={"value": "8", "name": "Free"})]
    qb = _qb(rows)
    qb.objects[("Item", "8")] = {"Id": "8", "Name": "Free", "Type": "Service", "UnitPrice": 0}
    with pytest.raises(InvoiceBuildError) as excinfo:
        create_invoice_from_time(qb, customer_id="42")
    message = str(excinfo.value)
    assert "2" in message and "3" in message
    assert "service item" in message and "rate" in message
    assert qb.created == []


def test_mark_not_billable_updates_entries_after_invoice():
    rows = [_entry("1"), _entry("2")]
    qb = _qb(rows)
    create_invoice_from_time(qb, customer_id="42", mark_not_billable_on_invoice=True)
    assert [u[0] for u in qb.updated] == ["TimeActivity", "TimeActivity"]
    bodies = [u[1] for u in qb.updated]
    assert [b["Id"] for b in bodies] == ["1", "2"]
    assert all(b["BillableStatus"] == "NotBillable" for b in bodies)
    assert all(b["SyncToken"] == "3" for b in bodies)


def test_default_does_not_update_entries():
    qb = _qb([_entry("1")])
    create_invoice_from_time(qb, customer_id="42")
    assert qb.updated == []
