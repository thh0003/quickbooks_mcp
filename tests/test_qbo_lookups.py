from qbo_mcp.qbo import search_customers, search_service_items, search_workers
from tests.conftest import FakeQuickBooks


def _rows_by_entity(select: str):
    if " FROM Customer " in select or select.endswith("FROM Customer"):
        return [
            {"Id": "1", "DisplayName": "Acme Corp", "CompanyName": "Acme Corporation", "Active": True},
            {"Id": "2", "DisplayName": "O'Brien Ltd", "CompanyName": None, "Active": True},
        ]
    if " FROM Employee" in select:
        return [{"Id": "55", "DisplayName": "Sam Lee", "Active": True}]
    if " FROM Vendor" in select:
        return [{"Id": "77", "DisplayName": "Samantha Contracting", "Active": True}]
    if " FROM Item" in select:
        return [{"Id": "9", "Name": "Consulting", "UnitPrice": 95, "Type": "Service", "Active": True}]
    return []


def test_search_customers_returns_matches_and_escapes_apostrophe():
    qb = FakeQuickBooks(query_handler=_rows_by_entity)
    results = search_customers(qb, "O'Brien")
    assert [r.display_name for r in results] == ["Acme Corp", "O'Brien Ltd"]
    assert results[0].company_name == "Acme Corporation"
    assert results[1].company_name is None
    assert results[0].active is True
    assert len(qb.queries) == 1
    assert "DisplayName LIKE '%O\\'Brien%'" in qb.queries[0]
    assert "Active = true" in qb.queries[0]
    assert "MAXRESULTS 25" in qb.queries[0]


def test_search_customers_empty_query_lists_active():
    qb = FakeQuickBooks(query_handler=_rows_by_entity)
    search_customers(qb, "")
    assert "LIKE" not in qb.queries[0]
    assert "Active = true" in qb.queries[0]


def test_search_workers_merges_employees_and_vendors():
    qb = FakeQuickBooks(query_handler=_rows_by_entity)
    results = search_workers(qb, "sam")
    assert [(r.id, r.display_name, r.kind) for r in results] == [
        ("55", "Sam Lee", "Employee"),
        ("77", "Samantha Contracting", "Vendor"),
    ]
    assert any(" FROM Employee" in q for q in qb.queries)
    assert any(" FROM Vendor" in q for q in qb.queries)
    assert all("DisplayName LIKE '%sam%'" in q for q in qb.queries)


def test_search_service_items_filters_type_service():
    qb = FakeQuickBooks(query_handler=_rows_by_entity)
    results = search_service_items(qb, "cons")
    assert results[0].id == "9"
    assert results[0].name == "Consulting"
    assert results[0].unit_price == 95.0
    assert results[0].type == "Service"
    assert "Type = 'Service'" in qb.queries[0]
    assert "Name LIKE '%cons%'" in qb.queries[0]
