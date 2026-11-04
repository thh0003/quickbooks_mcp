import json
import stat

import pytest
from quickbooks.exceptions import AuthorizationException

from qbo_mcp.auth import (
    NotAuthorizedError,
    Tokens,
    TokenStore,
    build_quickbooks_client,
    handle_callback,
)
from qbo_mcp.config import Settings
from tests.conftest import FakeAuthClient, make_auth_client_error


@pytest.fixture
def settings(tmp_path):
    return Settings(
        client_id="cid",
        client_secret="secret",
        environment="sandbox",
        redirect_uri="http://localhost:8765/callback",
        token_path=tmp_path / "nested" / "tokens-sandbox.json",
    )


@pytest.fixture
def saved_tokens():
    return Tokens(
        access_token="access-0",
        refresh_token="refresh-0",
        realm_id="12345",
        environment="sandbox",
        saved_at="2026-11-03T00:00:00+00:00",
    )


# --- TokenStore ---------------------------------------------------------------


def test_token_store_round_trip_with_private_permissions(settings, saved_tokens):
    store = TokenStore(settings.token_path)
    store.save(saved_tokens)
    mode = stat.S_IMODE(settings.token_path.stat().st_mode)
    assert mode == 0o600
    loaded = store.load()
    assert loaded == saved_tokens
    assert set(json.loads(settings.token_path.read_text())) == {
        "access_token",
        "refresh_token",
        "realm_id",
        "environment",
        "saved_at",
    }


def test_token_store_load_missing_returns_none(tmp_path):
    assert TokenStore(tmp_path / "missing.json").load() is None


# --- Authorisation callback ---------------------------------------------------


def test_callback_rejects_mismatched_state(settings):
    auth_client = FakeAuthClient(state_token="expected")
    store = TokenStore(settings.token_path)
    with pytest.raises(ValueError, match="state"):
        handle_callback(auth_client, {"state": "other", "code": "abc", "realmId": "99"}, store, settings.environment)
    assert store.load() is None
    assert auth_client.bearer_calls == []


def test_callback_exchanges_code_and_saves_tokens(settings):
    auth_client = FakeAuthClient(state_token="expected")
    store = TokenStore(settings.token_path)
    tokens = handle_callback(auth_client, {"state": "expected", "code": "abc", "realmId": "99"}, store, settings.environment)
    assert auth_client.bearer_calls == [("abc", "99")]
    assert tokens.realm_id == "99"
    assert tokens.access_token == "access-for-abc"
    assert tokens.refresh_token == "refresh-for-abc"
    assert store.load() == tokens


def test_callback_reports_provider_error(settings):
    auth_client = FakeAuthClient(state_token="expected")
    store = TokenStore(settings.token_path)
    with pytest.raises(ValueError, match="access_denied"):
        handle_callback(auth_client, {"state": "expected", "error": "access_denied"}, store, settings.environment)


# --- Client factory -----------------------------------------------------------


def test_build_client_without_tokens_raises(settings):
    with pytest.raises(NotAuthorizedError, match="qbo-mcp authorize"):
        build_quickbooks_client(settings, TokenStore(settings.token_path), auth_client_factory=lambda **kw: FakeAuthClient(**kw))


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def _client_with_responses(settings, saved_tokens, responses):
    """Build a client whose HTTP layer is replaced by a scripted list of responses."""
    store = TokenStore(settings.token_path)
    store.save(saved_tokens)
    created = {}

    def factory(**kwargs):
        created["auth_client"] = FakeAuthClient(**kwargs)
        return created["auth_client"]

    qb = build_quickbooks_client(settings, store, auth_client_factory=factory)
    calls = []

    def fake_process_request(request_type, url, headers=None, params=None, data=None):
        # The real process_request reads the bearer token from the OAuth2Session, so a
        # refresh must rebuild that session for the new token to be sent.
        calls.append("Bearer " + qb.session.access_token)
        return responses.pop(0)

    qb.process_request = fake_process_request
    return qb, store, created["auth_client"], calls


def test_build_client_starts_with_fresh_access_token_and_persists_rotation(settings, saved_tokens):
    qb, store, auth_client, _ = _client_with_responses(settings, saved_tokens, [])
    # The factory must be given no access token so python-quickbooks refreshes at session start.
    assert auth_client.refresh_calls == ["refresh-0"]
    assert qb.company_id == "12345"
    assert qb.minorversion == 75
    assert qb.use_decimal is True
    on_disk = store.load()
    assert on_disk.refresh_token == "refresh-1"
    assert on_disk.access_token == "access-1"


def test_make_request_refreshes_once_and_retries_on_401(settings, saved_tokens):
    ok = _Resp(200, json.dumps({"Customer": {"Id": "1"}}))
    qb, store, auth_client, auth_headers = _client_with_responses(settings, saved_tokens, [_Resp(401, "expired"), ok])
    result = qb.make_request("GET", "https://example/customer/1")
    assert result == {"Customer": {"Id": "1"}}
    assert auth_client.refresh_calls == ["refresh-0", "refresh-1"]
    assert auth_headers == ["Bearer access-1", "Bearer access-2"]
    assert store.load().refresh_token == "refresh-2"


def test_make_request_second_401_propagates(settings, saved_tokens):
    qb, store, auth_client, _ = _client_with_responses(settings, saved_tokens, [_Resp(401, "a"), _Resp(401, "b")])
    with pytest.raises(AuthorizationException):
        qb.make_request("GET", "https://example/customer/1")
    assert len(auth_client.refresh_calls) == 2


def test_make_request_refresh_failure_propagates_and_store_unchanged(settings, saved_tokens):
    qb, store, auth_client, _ = _client_with_responses(settings, saved_tokens, [_Resp(401, "expired")])
    before = store.load()
    auth_client.refresh_error = make_auth_client_error()
    from intuitlib.exceptions import AuthClientError

    with pytest.raises(AuthClientError):
        qb.make_request("GET", "https://example/customer/1")
    assert store.load() == before
