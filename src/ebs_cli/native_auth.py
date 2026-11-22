"""Globus Native App login for the CLI, without a confidential client."""

import base64
import hashlib
import hmac
import json
import secrets
import socket
import threading
import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from ebs_cli.client import normalize_server_url
from ebs_cli.config import validate_token
from ebs_cli.errors import CLIError

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 53682
CALLBACK_PATH = "/oauth/callback"
REDIRECT_URI = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}{CALLBACK_PATH}"
GLOBUS_SCOPE = "openid profile email offline_access"
NERSC_IDP = "b2b0a937-3c52-40de-ad95-1b2a76285912"
NERSC_DOMAIN = "nersc.gov"
GLOBUS_AUTHORIZATION_ENDPOINT = "https://auth.globus.org/v2/oauth2/authorize"
GLOBUS_TOKEN_ENDPOINT = "https://auth.globus.org/v2/oauth2/token"
REVOCATION_ENDPOINT = f"{GLOBUS_TOKEN_ENDPOINT}/revoke"
CLI_TOKEN_LABEL = "ebs-cli-native-login"
CALLBACK_WAIT_SECONDS = 180.0
HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 32768
MAX_CALLBACK_TARGET_BYTES = 8192


@dataclass(frozen=True)
class NativeClientConfig:
    client_id: str
    authorization_endpoint: str
    token_endpoint: str


@dataclass(frozen=True)
class LoginCredentials:
    """Values to persist together; the access token is deliberately absent."""

    token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    client_id: str


@dataclass(frozen=True)
class _ProviderTokens:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _Response:
    status_code: int
    content_type: str
    body: bytes


class _CallbackPathMismatch(Exception):
    pass


class _CallbackRejected(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def generate_oauth_material() -> tuple[str, str, str]:
    """Return fresh random state, a PKCE verifier, and its S256 challenge."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    if not 43 <= len(state) <= 512 or not 43 <= len(verifier) <= 128:
        raise CLIError("Could not generate secure login parameters. Retry browser login.")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return state, verifier, challenge


def authorization_url(
    config: NativeClientConfig,
    *,
    state: str,
    challenge: str,
) -> str:
    """Build the documented authorization-code + S256 Native App request."""
    _validate_endpoint(config.authorization_endpoint, allow_http=False)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": GLOBUS_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "required_idp": NERSC_IDP,
            "session_required_single_domain": NERSC_DOMAIN,
        }
    )
    return f"{config.authorization_endpoint}?{query}"


def parse_callback_target(target: str, expected_state: str) -> str:
    """Validate one origin-form callback and return its single-use code."""
    if len(target.encode("utf-8")) > MAX_CALLBACK_TARGET_BYTES:
        raise _CallbackRejected("The loopback callback was malformed.")
    try:
        target_bytes = target.encode("ascii")
        parsed = urlsplit(target_bytes.decode("ascii"))
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=8,
        )
    except (UnicodeError, ValueError):
        raise _CallbackRejected("The loopback callback was malformed.") from None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise _CallbackRejected("The loopback callback was malformed.")
    if parsed.path != CALLBACK_PATH:
        raise _CallbackPathMismatch
    if not pairs or len({name for name, _value in pairs}) != len(pairs):
        raise _CallbackRejected("The loopback callback must contain one response.")
    values = dict(pairs)
    returned_state = values.get("state", "")
    if (
        not isinstance(returned_state, str)
        or not returned_state
        or len(returned_state) > 512
        or not returned_state.isascii()
        or not isinstance(expected_state, str)
        or not expected_state.isascii()
    ):
        raise _CallbackRejected("The loopback callback state was invalid.", 400)
    # compare_digest rejects mixed str/non-ASCII input before doing any
    # comparison. The ASCII shape check above turns that into a clean denial.
    if not hmac.compare_digest(returned_state, expected_state):
        raise _CallbackRejected("The loopback callback state did not match. Login was not completed.", 403)
    if "error" in values:
        if set(values) - {"error", "state", "error_description", "error_uri"}:
            raise _CallbackRejected("The loopback error callback was malformed.")
        if values["error"] == "access_denied":
            raise _CallbackRejected("Globus authorization was denied. Nothing was saved.", 403)
        raise _CallbackRejected("Globus authorization failed. Retry browser login.", 403)
    if set(values) != {"code", "state"}:
        raise _CallbackRejected("The loopback callback must contain exactly one code and state.")
    code = values["code"]
    if (
        not code
        or len(code) > 4096
        or any(not character.isascii() or not character.isprintable() for character in code)
    ):
        raise _CallbackRejected("The loopback callback contained an invalid authorization code.")
    return code


class _LoopbackHTTPServer(HTTPServer):
    """Track and forcibly interrupt the one active loopback connection."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._active_lock = threading.Lock()
        self._active_request: socket.socket | None = None
        self.deadline = 0.0
        self.aborted = False
        self.finished = threading.Event()
        super().__init__(*args, **kwargs)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        with self._active_lock:
            if self.aborted:
                with suppress(OSError):
                    request.close()
                raise OSError("login callback is closed")
            self._active_request = request
        request.settimeout(max(0.001, min(2.0, self.deadline - time.monotonic())))
        return request, client_address

    def close_request(self, request: socket.socket) -> None:
        try:
            super().close_request(request)
        finally:
            with self._active_lock:
                if self._active_request is request:
                    self._active_request = None

    def expired(self) -> bool:
        return self.aborted or time.monotonic() >= self.deadline

    def abort(self) -> None:
        with self._active_lock:
            self.aborted = True
            request = self._active_request
        if request is not None:
            with suppress(OSError):
                request.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                request.close()

    def handle_error(self, _request: Any, _client_address: Any) -> None:
        # Never print a malformed request/backtrace (which may include secrets).
        return


class _CallbackHandler(BaseHTTPRequestHandler):
    """Never reflect request targets or log callback query parameters."""

    server_version = "EBSLogin"
    sys_version = ""

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def log_error(self, _format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.server.expired():  # type: ignore[attr-defined]
            self.server.callback_done = True  # type: ignore[attr-defined]
            return
        try:
            code = parse_callback_target(self.path, self.server.expected_state)  # type: ignore[attr-defined]
        except _CallbackPathMismatch:
            self._respond(404, "Not found. Return to your terminal.")
            return
        except _CallbackRejected as exc:
            self.server.callback_error = exc  # type: ignore[attr-defined]
            self.server.callback_done = True  # type: ignore[attr-defined]
            self._respond(exc.status, "Login was not completed. Return to your terminal.")
            return
        if self.server.expired():  # type: ignore[attr-defined]
            self.server.callback_done = True  # type: ignore[attr-defined]
            return
        self.server.callback_code = code  # type: ignore[attr-defined]
        self.server.callback_done = True  # type: ignore[attr-defined]
        self._respond(200, "EBS login received. Return to your terminal. You may close this window.")

    def do_POST(self) -> None:
        self._respond(405, "Method not allowed.")

    def _respond(self, status: int, message: str) -> None:
        body = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>EBS login</title></head><body>"
            f"{message}</body></html>"
        ).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


class LoopbackCallback:
    """One fixed, IPv4-loopback-only receiver with deterministic cleanup."""

    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self._server: _LoopbackHTTPServer | None = None
        self._watchdog: threading.Thread | None = None

    def __enter__(self) -> "LoopbackCallback":
        try:
            server = _LoopbackHTTPServer((LOOPBACK_HOST, LOOPBACK_PORT), _CallbackHandler)
        except OSError:
            raise CLIError(
                f"Could not open the private login callback at {REDIRECT_URI}. "
                "Close any other EBS login and retry."
            ) from None
        server.expected_state = self.expected_state  # type: ignore[attr-defined]
        server.callback_done = False  # type: ignore[attr-defined]
        server.callback_code = None  # type: ignore[attr-defined]
        server.callback_error = None  # type: ignore[attr-defined]
        self._server = server
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        server = self._server
        if server is not None:
            server.finished.set()
            server.abort()
            server.server_close()
            self._server = None
        self._join_watchdog()

    def _join_watchdog(self) -> None:
        watchdog = self._watchdog
        self._watchdog = None
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join()

    def wait(self, timeout: float = CALLBACK_WAIT_SECONDS) -> str:
        server = self._server
        if server is None:
            raise CLIError("The private login callback is not running.")
        deadline = time.monotonic() + timeout
        server.deadline = deadline

        def enforce_deadline() -> None:
            if not server.finished.wait(max(0.0, deadline - time.monotonic())):
                # Socket inactivity timeouts reset on every dribbled byte.
                # Closing the active descriptor is the absolute backstop.
                server.abort()

        watchdog = threading.Thread(
            target=enforce_deadline,
            name="ebs-callback-deadline",
            daemon=True,
        )
        self._watchdog = watchdog
        watchdog.start()
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise CLIError("Browser login timed out. Nothing was saved. Retry 'ebs auth login'.")
                if server.callback_done:  # type: ignore[attr-defined]
                    break
                remaining = deadline - time.monotonic()
                server.timeout = max(0.001, min(0.05, remaining))
                try:
                    server.handle_request()
                except OSError:
                    continue
            # A callback completed at the deadline is never accepted late.
            if time.monotonic() >= deadline:
                raise CLIError("Browser login timed out. Nothing was saved. Retry 'ebs auth login'.")
            error = server.callback_error  # type: ignore[attr-defined]
            if error is not None:
                raise CLIError(error.message)
            code = server.callback_code  # type: ignore[attr-defined]
            if not isinstance(code, str):
                raise CLIError("Browser login did not complete. Nothing was saved.")
            return code
        finally:
            server.finished.set()
            server.abort()
            self._join_watchdog()


def _is_canonical_endpoint(value: str, expected: str) -> bool:
    try:
        parsed = urlsplit(value)
        canonical = urlsplit(expected)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == canonical.hostname
        and port in (None, 443)
        and parsed.path == canonical.path
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _validate_provider_configuration(config: NativeClientConfig, *, allow_custom: bool) -> None:
    endpoints = (
        (config.authorization_endpoint, GLOBUS_AUTHORIZATION_ENDPOINT),
        (config.token_endpoint, GLOBUS_TOKEN_ENDPOINT),
    )
    for value, expected in endpoints:
        try:
            _validate_endpoint(value, allow_http=allow_custom)
        except CLIError:
            if not allow_custom:
                raise CLIError(
                    "The Globus Native App configuration contains unsupported provider endpoints."
                ) from None
            raise
        if not allow_custom and not _is_canonical_endpoint(value, expected):
            raise CLIError("The Globus Native App configuration contains unsupported provider endpoints.")


def discover_native_client(
    server_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> NativeClientConfig:
    endpoint = f"{normalize_server_url(server_url)}/api/v1/auth/cli-config"
    response = _request(
        "GET",
        endpoint,
        operation="EBS CLI login discovery",
        transport=transport,
        headers={"Accept": "application/json"},
    )
    if response.status_code != 200:
        raise CLIError(
            "Browser login is unavailable on this EBS server. "
            "Ask an administrator to configure the Globus Native App, or use 'ebs auth login --token'."
        )
    document = _json_document(response, "EBS CLI login discovery")
    if set(document) != {"client_id", "authorization_endpoint", "token_endpoint"}:
        raise CLIError("EBS CLI login discovery returned an invalid response.")
    client_id = document["client_id"]
    authorization_endpoint = document["authorization_endpoint"]
    token_endpoint = document["token_endpoint"]
    if not all(isinstance(value, str) for value in (client_id, authorization_endpoint, token_endpoint)):
        raise CLIError("EBS CLI login discovery returned an invalid response.")
    client_id = _validated_credential(client_id, "Native App client ID", secret=False)
    config = NativeClientConfig(client_id, authorization_endpoint, token_endpoint)
    try:
        _validate_provider_configuration(config, allow_custom=transport is not None)
    except CLIError:
        raise CLIError("EBS CLI login discovery returned unsafe provider endpoints.") from None
    return config


def browser_login(
    server_url: str,
    *,
    no_browser: bool = False,
    show_url: Callable[[str], None] | None = None,
    transport: httpx.BaseTransport | None = None,
    callback_factory: Callable[[str], Any] = LoopbackCallback,
    open_browser: Callable[[str], bool] = webbrowser.open,
    wait_seconds: float = CALLBACK_WAIT_SECONDS,
) -> LoginCredentials:
    """Complete browser login, then mint the personal token before saving."""
    config = discover_native_client(server_url, transport=transport)
    _validate_provider_configuration(config, allow_custom=transport is not None)
    state, verifier, challenge = generate_oauth_material()
    url = authorization_url(config, state=state, challenge=challenge)
    # Test transports explicitly permit loopback/documentation HTTP fakes; real
    # requests require HTTPS before either endpoint or callback is contacted.
    _validate_endpoint(
        normalize_server_url(server_url),
        allow_http=transport is not None,
    )
    with callback_factory(state) as callback:
        if no_browser:
            if show_url is not None:
                show_url(url)
        else:
            try:
                opened = open_browser(url)
            except Exception:
                opened = False
            if not opened:
                raise CLIError(
                    "Could not open a web browser. Retry with 'ebs auth login --no-browser' "
                    "and open the displayed URL yourself."
                )
        code = callback.wait(wait_seconds)
    tokens = _exchange_code(config, code=code, verifier=verifier, transport=transport)
    personal_token = _mint_personal_token(
        server_url,
        tokens.access_token,
        transport=transport,
    )
    if tokens.refresh_token is None:  # Guarded by strict initial-exchange parsing.
        raise CLIError("Globus did not return a refresh credential. Nothing was saved.")
    return LoginCredentials(personal_token, tokens.refresh_token, config.client_id)


def refresh_login(
    server_url: str,
    refresh_token: str,
    client_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> LoginCredentials:
    """Use a saved grant only for this explicit command, never on API calls."""
    config = discover_native_client(server_url, transport=transport)
    _validate_provider_configuration(config, allow_custom=transport is not None)
    if not hmac.compare_digest(config.client_id, client_id):
        raise CLIError(
            "The EBS Native App configuration changed. Run 'ebs auth login' to create a new login."
        )
    response = _request(
        "POST",
        config.token_endpoint,
        operation="Globus token refresh",
        transport=transport,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    if response.status_code in {400, 401, 403}:
        raise CLIError(
            "The saved Globus login was rejected or expired. "
            "Saved credentials were left unchanged; run 'ebs auth login' to create a new login."
        )
    if response.status_code != 200:
        raise CLIError("Globus token refresh is unavailable. Saved credentials were left unchanged.")
    tokens = _parse_provider_tokens(response, require_refresh=False, refreshing=True)
    personal_token = _mint_personal_token(
        server_url,
        tokens.access_token,
        transport=transport,
    )
    return LoginCredentials(personal_token, tokens.refresh_token or refresh_token, client_id)


def revoke_refresh_token(
    refresh_token: str,
    client_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Revoke only this Native App refresh grant, never browser/web sessions."""
    response = _request(
        "POST",
        REVOCATION_ENDPOINT,
        operation="Globus credential revocation",
        transport=transport,
        data={"token": refresh_token, "client_id": client_id},
    )
    if response.status_code != 200:
        raise CLIError("Globus did not confirm revocation of the saved grant.")


def _exchange_code(
    config: NativeClientConfig,
    *,
    code: str,
    verifier: str,
    transport: httpx.BaseTransport | None,
) -> _ProviderTokens:
    response = _request(
        "POST",
        config.token_endpoint,
        operation="Globus code exchange",
        transport=transport,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config.client_id,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    if response.status_code != 200:
        raise CLIError("Globus rejected the browser authorization. Nothing was saved. Retry login.")
    return _parse_provider_tokens(response, require_refresh=True, refreshing=False)


def _mint_personal_token(
    server_url: str,
    access_token: str,
    *,
    transport: httpx.BaseTransport | None,
) -> str:
    endpoint = f"{normalize_server_url(server_url)}/api/v1/auth/cli/token"
    response = _request(
        "POST",
        endpoint,
        operation="EBS personal token creation",
        transport=transport,
        data=None,
        json_body={"name": CLI_TOKEN_LABEL},
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    if response.status_code != 201:
        raise CLIError(
            "EBS could not create a personal token from the Globus identity. "
            "Nothing was saved. Check NERSC account synchronization or contact support."
        )
    document = _json_document(response, "EBS personal token creation")
    token = document.get("token")
    if not isinstance(token, str):
        raise CLIError("EBS returned an invalid personal-token response. Nothing was saved.")
    try:
        return validate_token(token)
    except CLIError:
        raise CLIError("EBS returned an invalid personal-token response. Nothing was saved.") from None


def _parse_provider_tokens(
    response: _Response, *, require_refresh: bool, refreshing: bool
) -> _ProviderTokens:
    document = _json_document(response, "Globus token exchange")
    access_token = document.get("access_token")
    refresh_token = document.get("refresh_token")
    try:
        access_token = _validated_credential(access_token, "access token")
        if require_refresh:
            refresh_token = _validated_credential(refresh_token, "refresh token")
        elif refresh_token is not None:
            refresh_token = _validated_credential(refresh_token, "refresh token")
        expires_in = document.get("expires_in")
        if expires_in is not None and (type(expires_in) is not int or expires_in < 0):
            raise ValueError
        token_type = document.get("token_type")
        if token_type is not None and (not isinstance(token_type, str) or token_type.lower() != "bearer"):
            raise ValueError
    except (CLIError, ValueError):
        action = "refresh" if refreshing else "code exchange"
        outcome = "Saved credentials were left unchanged" if refreshing else "Nothing was saved"
        raise CLIError(f"Globus returned an invalid {action} response. {outcome}.") from None
    return _ProviderTokens(access_token, refresh_token)


def _request(
    method: str,
    url: str,
    *,
    operation: str,
    transport: httpx.BaseTransport | None,
    data: dict[str, str] | None = None,
    json_body: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> _Response:
    _validate_endpoint(url, allow_http=transport is not None)
    try:
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=min(5.0, HTTP_TIMEOUT_SECONDS))
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            with client.stream(
                method,
                url,
                data=data,
                json=json_body,
                headers=headers,
            ) as response:
                # Error bodies can contain echoed form data. Never read or
                # include them in diagnostics; only the numeric status remains.
                if response.status_code < 200 or response.status_code >= 300:
                    return _Response(response.status_code, "", b"")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise CLIError(f"{operation} returned an oversized response.")
                    chunks.append(chunk)
                return _Response(
                    response.status_code,
                    response.headers.get("content-type", ""),
                    b"".join(chunks),
                )
    except CLIError:
        raise
    except httpx.TimeoutException:
        raise CLIError(f"{operation} timed out. Retry later.") from None
    except httpx.HTTPError:
        raise CLIError(f"{operation} could not be reached. Check the network and retry.") from None


def _json_document(response: _Response, operation: str) -> dict[str, Any]:
    media_type = response.content_type.partition(";")[0].strip().lower()
    if media_type != "application/json" or not response.body:
        raise CLIError(f"{operation} returned an invalid JSON response.")
    try:
        document = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CLIError(f"{operation} returned an invalid JSON response.") from None
    if not isinstance(document, dict):
        raise CLIError(f"{operation} returned an invalid JSON response.")
    return document


def _validated_credential(value: Any, label: str, *, secret: bool = True) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16384
        or any(not character.isascii() or not character.isprintable() for character in value)
    ):
        if secret:
            raise CLIError(f"Invalid {label} received.")
        raise CLIError(f"Invalid public {label}.")
    return value


def _validate_endpoint(url: str, *, allow_http: bool) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise CLIError("An authentication endpoint URL is invalid.") from None
    if (
        parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise CLIError(
            "Authentication endpoints must use HTTPS URLs without credentials, queries, or fragments."
        )
