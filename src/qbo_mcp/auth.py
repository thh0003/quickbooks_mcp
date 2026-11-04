"""OAuth 2.0 authorisation, token persistence and QuickBooks client construction."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from intuitlib.client import AuthClient
from intuitlib.enums import Scopes
from quickbooks import QuickBooks
from quickbooks.exceptions import AuthorizationException

from .config import Settings

log = logging.getLogger(__name__)

MINOR_VERSION = 75


class NotAuthorizedError(RuntimeError):
    """No usable tokens exist; the user must run ``qbo-mcp authorize``."""


class AuthClientLike(Protocol):
    client_id: str
    environment: str
    access_token: str | None
    refresh_token: str | None
    realm_id: str | None
    state_token: str | None

    def get_authorization_url(self, scopes: list[Any], state_token: str | None = None) -> str: ...

    def get_bearer_token(self, auth_code: str, realm_id: str | None = None) -> None: ...

    def refresh(self, refresh_token: str | None = None) -> None: ...


AuthClientFactory = Callable[..., AuthClientLike]


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    realm_id: str
    environment: str
    saved_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TokenStore:
    """Reads and writes the token file with owner-only permissions."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Tokens | None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        return Tokens(**{key: raw[key] for key in Tokens.__dataclass_fields__})

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(asdict(tokens), handle, indent=2)
                handle.write("\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _default_auth_client_factory(**kwargs: Any) -> AuthClientLike:
    return AuthClient(**kwargs)


def make_auth_client(settings: Settings, factory: AuthClientFactory | None = None, **overrides: Any) -> AuthClientLike:
    factory = factory or _default_auth_client_factory
    return factory(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        redirect_uri=settings.redirect_uri,
        environment=settings.environment,
        **overrides,
    )


# --- Authorisation flow -------------------------------------------------------


def handle_callback(auth_client: AuthClientLike, query: dict[str, str], store: TokenStore, environment: str) -> Tokens:
    """Validate the OAuth callback parameters, exchange the code and persist tokens."""
    if "error" in query:
        raise ValueError(f"Intuit returned an error: {query['error']} {query.get('error_description', '')}".strip())
    if query.get("state") != auth_client.state_token:
        raise ValueError("Callback state does not match the state sent to Intuit; refusing to continue.")
    code = query.get("code")
    realm_id = query.get("realmId")
    if not code or not realm_id:
        raise ValueError("Callback is missing the 'code' or 'realmId' parameter.")

    auth_client.get_bearer_token(code, realm_id=realm_id)
    if not auth_client.access_token or not auth_client.refresh_token:
        raise ValueError("Token exchange did not return an access and refresh token.")

    tokens = Tokens(
        access_token=auth_client.access_token,
        refresh_token=auth_client.refresh_token,
        realm_id=str(auth_client.realm_id or realm_id),
        environment=environment,
        saved_at=_now_iso(),
    )
    store.save(tokens)
    return tokens


def _query_from_path(path: str) -> dict[str, str]:
    parsed = parse_qs(urlparse(path).query)
    return {key: values[0] for key, values in parsed.items() if values}


def run_authorization_flow(
    settings: Settings,
    store: TokenStore,
    *,
    auth_client: AuthClientLike | None = None,
    open_browser: bool = True,
    timeout: float = 300.0,
) -> Tokens:
    """Print the consent URL, wait for the browser callback once, and persist tokens."""
    auth_client = auth_client or make_auth_client(settings)
    url = auth_client.get_authorization_url([Scopes.ACCOUNTING])

    redirect = urlparse(settings.redirect_uri)
    host = redirect.hostname or "localhost"
    port = redirect.port or 80
    callback_path = redirect.path or "/"

    outcome: dict[str, Any] = {}
    done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if urlparse(self.path).path != callback_path:
                self.send_response(404)
                self.end_headers()
                return
            try:
                outcome["tokens"] = handle_callback(auth_client, _query_from_path(self.path), store, settings.environment)
                body = "<h1>QuickBooks connected.</h1><p>You can close this tab and return to the terminal.</p>"
                status = 200
            except Exception as exc:  # report to the browser and the terminal, then stop
                outcome["error"] = exc
                body = f"<h1>Authorisation failed.</h1><p>{exc}</p>"
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            done.set()

        def log_message(self, format: str, *args: Any) -> None:  # keep stdout clean
            log.debug("callback server: " + format, *args)

    try:
        server = HTTPServer((host, port), CallbackHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot listen on {host}:{port} for the OAuth callback ({exc}). "
            "Close the program using that port or set QBO_REDIRECT_URI to a free port "
            "(and register the same URI in the Intuit app)."
        ) from exc

    print("Open this URL in your browser to connect QuickBooks:", file=sys.stderr)
    print(url, file=sys.stderr)
    print(f"Waiting for the callback on {settings.redirect_uri} ...", file=sys.stderr)
    if open_browser:
        webbrowser.open(url)

    server.timeout = 1.0
    try:
        deadline = datetime.now(timezone.utc).timestamp() + timeout
        while not done.is_set():
            if datetime.now(timezone.utc).timestamp() > deadline:
                raise TimeoutError("Timed out waiting for the Intuit callback.")
            server.handle_request()
    finally:
        server.server_close()

    if "error" in outcome:
        raise RuntimeError(f"Authorisation failed: {outcome['error']}") from outcome["error"]
    tokens: Tokens = outcome["tokens"]
    print(f"Connected to QuickBooks company {tokens.realm_id} ({tokens.environment}). Tokens saved to {store.path}.", file=sys.stderr)
    return tokens


# --- Client factory -----------------------------------------------------------


class ManagedQuickBooks(QuickBooks):
    """``QuickBooks`` client that refreshes once on a 401 and keeps the token file in sync.

    ``QuickBooks.__new__`` performs all construction; ``_store`` is attached by
    :func:`build_quickbooks_client` right after. Intuit rotates the refresh token on
    every refresh, so the store is rewritten whenever the auth client's tokens change.
    """

    _store: TokenStore
    _environment: str

    def make_request(self, request_type, url, request_body=None, content_type="application/json", params=None, file_path=None, file_bytes=None, request_id=None):  # type: ignore[override]
        kwargs = dict(request_body=request_body, content_type=content_type, params=dict(params) if params else None, file_path=file_path, file_bytes=file_bytes, request_id=request_id)
        try:
            try:
                return super().make_request(request_type, url, **kwargs)
            except AuthorizationException:
                log.info("QuickBooks returned 401; refreshing the access token and retrying once")
                self._refresh_session()
                return super().make_request(request_type, url, **kwargs)
        finally:
            self.persist_tokens()

    def _refresh_session(self) -> None:
        self.auth_client.refresh(refresh_token=self.auth_client.refresh_token)
        # Rebuild the OAuth2Session so requests carry the new access token.
        self.refresh_token = self.auth_client.refresh_token
        self._start_session()

    def persist_tokens(self) -> None:
        current = self._store.load()
        access = self.auth_client.access_token
        refresh = self.auth_client.refresh_token
        if not access or not refresh:
            return
        if current and current.access_token == access and current.refresh_token == refresh:
            return
        self._store.save(
            Tokens(
                access_token=access,
                refresh_token=refresh,
                realm_id=str(self.company_id),
                environment=self._environment,
                saved_at=_now_iso(),
            )
        )


def build_quickbooks_client(
    settings: Settings,
    store: TokenStore,
    *,
    auth_client_factory: AuthClientFactory | None = None,
) -> ManagedQuickBooks:
    """Return a client bound to the stored realm, refreshing the access token at start."""
    tokens = store.load()
    if tokens is None:
        raise NotAuthorizedError(
            f"No QuickBooks tokens found at {store.path}. Run `uv run qbo-mcp authorize` first."
        )
    if tokens.environment != settings.environment:
        raise NotAuthorizedError(
            f"Stored tokens are for the {tokens.environment} environment but QBO_ENVIRONMENT is "
            f"{settings.environment}. Run `uv run qbo-mcp authorize` for this environment."
        )

    # access_token=None makes python-quickbooks refresh during _start_session(), so every
    # process start gets a fresh one-hour access token.
    auth_client = make_auth_client(
        settings,
        auth_client_factory,
        access_token=None,
        refresh_token=tokens.refresh_token,
        realm_id=tokens.realm_id,
    )
    qb = ManagedQuickBooks(
        auth_client=auth_client,
        refresh_token=tokens.refresh_token,
        company_id=tokens.realm_id,
        minorversion=MINOR_VERSION,
        use_decimal=True,
    )
    qb._store = store
    qb._environment = settings.environment
    qb.persist_tokens()
    return qb
