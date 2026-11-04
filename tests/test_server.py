import json

import pytest
from mcp import Client
from quickbooks.exceptions import ValidationException

from qbo_mcp import server
from qbo_mcp.auth import NotAuthorizedError
from tests.conftest import FakeQuickBooks

TOOL_NAMES = {
    "search_customers",
    "search_workers",
    "search_service_items",
    "create_time_entry",
    "list_time_entries",
    "create_invoice_from_time",
}

ENTRY_ROW = {
    "Id": "1",
    "TxnDate": "2026-10-15",
    "NameOf": "Employee",
    "EmployeeRef": {"value": "55", "name": "Sam Lee"},
    "CustomerRef": {"value": "42", "name": "Acme Corp"},
    "ItemRef": {"value": "9", "name": "Consulting"},
    "Hours": 1,
    "Minutes": 30,
    "HourlyRate": 120,
    "BillableStatus": "Billable",
    "Description": "Design review",
    "SyncToken": "0",
}

SAMPLE_CALLS = [
    ("search_customers", {"query": "acme"}),
    ("search_workers", {"query": "sam"}),
    ("search_service_items", {"query": ""}),
    (
        "create_time_entry",
        {"worker_kind": "Employee", "worker_id": "55", "customer_id": "42", "item_id": "9", "txn_date": "2026-10-15", "hours": 1, "minutes": 30},
    ),
    ("list_time_entries", {"customer_id": "42"}),
    ("create_invoice_from_time", {"customer_id": "42"}),
]


def _rows(select: str):
    if " FROM TimeActivity" in select:
        return [ENTRY_ROW]
    if " FROM Customer" in select:
        return [{"Id": "42", "DisplayName": "Acme Corp", "CompanyName": "Acme", "Active": True}]
    if " FROM Employee" in select:
        return [{"Id": "55", "DisplayName": "Sam Lee", "Active": True}]
    if " FROM Item" in select:
        return [{"Id": "9", "Name": "Consulting", "UnitPrice": 95, "Type": "Service", "Active": True}]
    return []


@pytest.fixture
def fake_qb(monkeypatch):
    qb = FakeQuickBooks(query_handler=_rows)
    qb.objects[("Item", "9")] = {"Id": "9", "Name": "Consulting", "Type": "Service", "UnitPrice": 95}
    qb.objects[("TimeActivity", "1")] = ENTRY_ROW
    monkeypatch.setattr(server, "get_client", lambda: qb)
    return qb


@pytest.fixture
async def client(fake_qb):
    async with Client(server.mcp) as c:
        yield c


@pytest.mark.anyio
async def test_lists_exactly_six_tools_with_schemas(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOL_NAMES
    assert all(t.description for t in tools)
    by_name = {t.name: t for t in tools}
    assert "customer_id" in by_name["list_time_entries"].input_schema["required"]
    assert "customer_id" in by_name["create_invoice_from_time"].input_schema["required"]
    assert by_name["create_time_entry"].output_schema is not None


@pytest.mark.anyio
async def test_create_time_entry_returns_structured_result(client, fake_qb):
    result = await client.call_tool("create_time_entry", dict(SAMPLE_CALLS[3][1]))
    assert result.is_error is False
    assert result.structured_content["id"] == "1000"
    assert result.structured_content["billable_status"] == "Billable"
    assert fake_qb.created[0][0] == "TimeActivity"


@pytest.mark.anyio
async def test_create_time_entry_rejects_both_duration_forms(client):
    args = dict(SAMPLE_CALLS[3][1])
    args.update(start_time="2026-10-15T09:00:00-05:00", end_time="2026-10-15T10:00:00-05:00")
    result = await client.call_tool("create_time_entry", args)
    assert result.is_error is True
    assert "either" in result.content[0].text


@pytest.mark.anyio
async def test_invoice_tool_reports_created_invoice(client, fake_qb):
    result = await client.call_tool("create_invoice_from_time", {"customer_id": "42"})
    assert result.is_error is False
    data = result.structured_content
    assert data["created"] is True
    assert data["invoice"]["invoice_id"] == "1000"
    assert data["invoice"]["lines"][0]["time_entry_id"] == "1"


@pytest.mark.anyio
async def test_invoice_tool_reports_no_unbilled_time(client, fake_qb):
    fake_qb.query_handler = lambda s: []
    result = await client.call_tool("create_invoice_from_time", {"customer_id": "42", "start_date": "2026-01-01"})
    assert result.is_error is False
    assert result.structured_content["created"] is False
    assert "no unbilled" in result.structured_content["message"].lower()
    assert fake_qb.created == []


@pytest.mark.anyio
async def test_not_authorized_maps_to_actionable_error(monkeypatch):
    def boom():
        raise NotAuthorizedError("no tokens")

    monkeypatch.setattr(server, "get_client", boom)
    async with Client(server.mcp) as c:
        for name, args in SAMPLE_CALLS:
            result = await c.call_tool(name, args)
            assert result.is_error is True, name
            assert "qbo-mcp authorize" in result.content[0].text, name


@pytest.mark.anyio
async def test_validation_exception_surfaces_message_and_detail(client, fake_qb):
    fake_qb.create_error = ValidationException("Invalid Reference Id", 2500, "Items element id 9 not found")
    result = await client.call_tool("create_time_entry", dict(SAMPLE_CALLS[3][1]))
    assert result.is_error is True
    text = result.content[0].text
    assert "Invalid Reference Id" in text and "Items element id 9 not found" in text


@pytest.mark.anyio
async def test_tools_write_nothing_to_stdout(client, capsys):
    for name, args in SAMPLE_CALLS:
        await client.call_tool(name, args)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_invoice_flag_passthrough(monkeypatch, fake_qb):
    """The module constant decides whether entries are flipped to NotBillable after invoicing."""
    import anyio

    monkeypatch.setattr(server, "MARK_NOT_BILLABLE_ON_INVOICE", True)

    async def run():
        async with Client(server.mcp) as c:
            await c.call_tool("create_invoice_from_time", {"customer_id": "42"})

    anyio.run(run)
    assert [u[1]["BillableStatus"] for u in fake_qb.updated] == ["NotBillable"]
    json.dumps(fake_qb.updated[0][1])  # payload is plain JSON
