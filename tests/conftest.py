"""Shared fakes for the qbo_mcp test suite.

Nothing here touches the network. ``FakeQuickBooks`` stands in for a
``quickbooks.QuickBooks`` client at the method boundary that python-quickbooks'
object mixins call (``query``, ``create_object``, ``update_object``,
``get_single_object``), so tests can assert on the exact JSON sent to QBO.
``FakeAuthClient`` stands in for ``intuitlib.client.AuthClient``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from intuitlib.exceptions import AuthClientError


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by AuthClientError."""

    def __init__(self, status_code: int = 400, content: bytes = b"invalid_grant"):
        self.status_code = status_code
        self.content = content
        self.headers: dict[str, str] = {}
        self.text = content.decode()


def make_auth_client_error(status_code: int = 400, content: bytes = b"invalid_grant") -> AuthClientError:
    return AuthClientError(_FakeResponse(status_code, content))


@dataclass
class FakeAuthClient:
    """Mimics the attributes and methods of ``intuitlib.client.AuthClient`` that qbo_mcp uses."""

    client_id: str = "cid"
    client_secret: str = "secret"
    redirect_uri: str = "http://localhost:8765/callback"
    environment: str = "sandbox"
    access_token: str | None = None
    refresh_token: str | None = None
    realm_id: str | None = None
    state_token: str | None = "state-123"
    refresh_calls: list[str | None] = field(default_factory=list)
    refresh_counter: int = 0
    refresh_error: Exception | None = None
    bearer_calls: list[tuple[str, str | None]] = field(default_factory=list)

    def get_authorization_url(self, scopes, state_token=None):
        self.state_token = state_token or self.state_token or "generated-state"
        return f"https://appcenter.intuit.com/connect/oauth2?state={self.state_token}"

    def get_bearer_token(self, auth_code, realm_id=None):
        self.bearer_calls.append((auth_code, realm_id))
        self.realm_id = realm_id or self.realm_id
        self.access_token = f"access-for-{auth_code}"
        self.refresh_token = f"refresh-for-{auth_code}"

    def refresh(self, refresh_token=None):
        self.refresh_calls.append(refresh_token)
        if self.refresh_error is not None:
            raise self.refresh_error
        self.refresh_counter += 1
        self.access_token = f"access-{self.refresh_counter}"
        self.refresh_token = f"refresh-{self.refresh_counter}"


@dataclass
class FakeQuickBooks:
    """Records calls made by python-quickbooks object mixins and returns canned JSON.

    ``query_handler`` receives the full ``SELECT`` statement and returns the list of
    entity dicts to wrap in a ``QueryResponse``. ``objects`` holds canned entities
    for ``get_single_object`` keyed by ``(entity_name, id)``.
    """

    query_handler: Callable[[str], list[dict[str, Any]]] = lambda select: []
    objects: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)
    created: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    updated: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    next_id: int = 1000
    create_error: Exception | None = None
    use_decimal: bool = False

    def query(self, select: str, params=None):
        self.queries.append(select)
        rows = self.query_handler(select)
        entity = select.split(" FROM ")[1].split(" ")[0]
        return {"QueryResponse": {entity: rows, "startPosition": 1, "maxResults": len(rows)}}

    def create_object(self, qbbo, request_body, _file_path=None, _file_bytes=None, request_id=None, params=None):
        body = json.loads(request_body)
        self.created.append((qbbo, body))
        if self.create_error is not None:
            raise self.create_error
        response = dict(body)
        response.setdefault("Id", str(self.next_id))
        response.setdefault("SyncToken", "0")
        self.next_id += 1
        if qbbo == "Invoice":
            response.setdefault("DocNumber", "1042")
            response.setdefault("TxnDate", "2026-11-03")
            response.setdefault("TotalAmt", sum(line.get("Amount", 0) for line in body.get("Line", [])))
        return {qbbo: response}

    def update_object(self, qbbo, request_body, _file_path=None, _file_bytes=None, request_id=None, params=None):
        body = json.loads(request_body)
        self.updated.append((qbbo, body))
        self.objects[(qbbo, str(body["Id"]))] = body
        return {qbbo: body}

    def get_single_object(self, qbbo, pk, params=None):
        key = (qbbo, str(pk))
        if key not in self.objects:
            from quickbooks.exceptions import ObjectNotFoundException

            raise ObjectNotFoundException(f"{qbbo} {pk} not found", 610, "Object Not Found")
        return {qbbo: self.objects[key]}
