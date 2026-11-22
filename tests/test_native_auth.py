import base64
import hashlib
import http.client
import json
import re
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from typer.testing import CliRunner

import ebs_cli.cli as cli_module
import ebs_cli.native_auth as native
from ebs_cli.cli import app
from ebs_cli.config import (
    GLOBUS_CLIENT_ID_KEY,
    GLOBUS_REFRESH_TOKEN_KEY,
    load_globus_credentials,
    load_token,
    logout,
    optional_globus_credentials,
    read_config,
    save_auth_credentials,
    save_project,
)
from ebs_cli.errors import CLIError
from ebs_cli.native_auth import LoginCredentials, LoopbackCallback, NativeClientConfig

CLIENT_ID = "native-client-test"
AUTHORIZATION_ENDPOINT = "https://auth.globus.test/v2/oauth2/authorize"
TOKEN_ENDPOINT = "https://auth.globus.test/v2/oauth2/token"
DISCOVERY = {
    "client_id": CLIENT_ID,
    "authorization_endpoint": AUTHORIZATION_ENDPOINT,
    "token_endpoint": TOKEN_ENDPOINT,
}
STATE = "test-state-that-is-long-enough-for-the-native-flow-123456789"
VERIFIER = "test-verifier-that-is-long-enough-for-S256-pkce-12345678901234567890"
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


class FakeCallback:
    def __init__(self, expected_state: str, result: str | Exception = "one-time-code") -> None:
        self.expected_state = expected_state
        self.result = result
        self.entered = False
        self.closed = False
        self.wait_seconds: float | None = None

    def __enter__(self) -> "FakeCallback":
        self.entered = True
        return self

    def __exit__(self, *_args: Any) -> None:
        self.closed = True

    def wait(self, timeout: float) -> str:
        self.wait_seconds = timeout
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_pkce_s256_and_state_are_fresh_and_well_formed(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter(("a" * 43, "b" * 64, "c" * 43, "d" * 64))
    monkeypatch.setattr(native.secrets, "token_urlsafe", lambda size: next(values))

    state, verifier, challenge = native.generate_oauth_material()
    second_state, second_verifier, second_challenge = native.generate_oauth_material()

    assert state == "a" * 43
    assert verifier == "b" * 64
    assert second_state == "c" * 43
    assert second_verifier == "d" * 64
    assert {state, second_state}.isdisjoint({verifier, second_verifier})
    assert challenge != second_challenge
    assert (
        challenge
        == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert "=" not in challenge
    assert re.fullmatch(r"[A-Za-z0-9_-]{43,128}", challenge)


def test_authorization_url_has_exact_redirect_scope_and_pkce() -> None:
    url = native.authorization_url(
        NativeClientConfig(CLIENT_ID, AUTHORIZATION_ENDPOINT, TOKEN_ENDPOINT),
        state=STATE,
        challenge=CHALLENGE,
    )
    query = parse_qs(urlsplit(url).query)

    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [native.REDIRECT_URI]
    assert query["scope"] == ["openid profile email offline_access"]
    assert query["code_challenge"] == [CHALLENGE]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [STATE]
    assert VERIFIER not in url
    assert native.REDIRECT_URI == "http://127.0.0.1:53682/oauth/callback"


def test_manual_authorization_url_uses_code_display_redirect_with_state_and_pkce() -> None:
    config = NativeClientConfig(CLIENT_ID, AUTHORIZATION_ENDPOINT, TOKEN_ENDPOINT)

    url = native.authorization_url(
        config,
        state=STATE,
        challenge=CHALLENGE,
        redirect_uri=native.CODE_DISPLAY_REDIRECT_URI,
    )
    query = parse_qs(urlsplit(url).query)

    assert native.CODE_DISPLAY_REDIRECT_URI == "https://auth.globus.org/v2/web/auth-code"
    assert query["redirect_uri"] == [native.CODE_DISPLAY_REDIRECT_URI]
    assert query["state"] == [STATE]
    assert query["code_challenge"] == [CHALLENGE]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid profile email offline_access"]
    assert VERIFIER not in url


def test_authorization_url_refuses_any_unregistered_redirect() -> None:
    with pytest.raises(CLIError, match="unsupported.*redirect"):
        native.authorization_url(
            NativeClientConfig(CLIENT_ID, AUTHORIZATION_ENDPOINT, TOKEN_ENDPOINT),
            state=STATE,
            challenge=CHALLENGE,
            redirect_uri="https://example.test/callback",
        )


def test_callback_state_comparison_is_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    observed = []
    original = native.hmac.compare_digest

    def observe(left: str, right: str) -> bool:
        observed.append((left, right))
        return original(left, right)

    monkeypatch.setattr(native.hmac, "compare_digest", observe)
    assert native.parse_callback_target(f"{native.CALLBACK_PATH}?code=abc&state={STATE}", STATE) == "abc"
    assert observed == [(STATE, STATE)]


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (f"{native.CALLBACK_PATH}?code=abc&state=wrong", "state did not match"),
        (f"{native.CALLBACK_PATH}?code=abc&code=other&state={STATE}", "one response"),
        (f"{native.CALLBACK_PATH}?code=abc", "state"),
        (f"{native.CALLBACK_PATH}?code=&state={STATE}", "invalid authorization code"),
        (f"{native.CALLBACK_PATH}?code=abc&state={STATE}&extra=value", "exactly one code and state"),
        (f"{native.CALLBACK_PATH}?code=abc&broken", "malformed"),
    ],
)
def test_callback_rejects_mismatched_and_malformed_responses(target: str, message: str) -> None:
    with pytest.raises(CLIError) as caught:
        try:
            native.parse_callback_target(target, STATE)
        except native._CallbackRejected as exc:
            raise CLIError(exc.message) from None
    assert message in str(caught.value)
    assert "one-time-secret" not in str(caught.value)


def test_callback_handles_denial_without_reflecting_provider_text() -> None:
    target = f"{native.CALLBACK_PATH}?error=access_denied&state={STATE}&error_description=private-detail"
    with pytest.raises(native._CallbackRejected, match="denied") as caught:
        native.parse_callback_target(target, STATE)
    assert "private-detail" not in str(caught.value)


def _request_loopback(target: str, results: list[tuple[int, str]]) -> None:
    connection = http.client.HTTPConnection(native.LOOPBACK_HOST, native.LOOPBACK_PORT, timeout=2)
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        results.append((response.status, response.read().decode("utf-8")))
    finally:
        connection.close()


def test_real_loopback_callback_success_and_cleanup() -> None:
    code = "private-one-time-code"
    target = f"{native.CALLBACK_PATH}?code={code}&state={STATE}"
    responses: list[tuple[int, str]] = []
    thread = threading.Thread(target=_request_loopback, args=(target, responses))
    with LoopbackCallback(STATE) as callback:
        thread.start()
        assert callback.wait(2) == code
        thread.join(2)
        assert callback._server is not None
    assert callback._server is None
    assert not thread.is_alive()
    assert responses[0][0] == 200
    assert code not in responses[0][1]
    assert STATE not in responses[0][1]


def test_real_loopback_callback_denial_timeout_and_cleanup() -> None:
    target = f"{native.CALLBACK_PATH}?error=access_denied&state={STATE}"
    responses: list[tuple[int, str]] = []
    thread = threading.Thread(target=_request_loopback, args=(target, responses))
    with LoopbackCallback(STATE) as callback:
        thread.start()
        with pytest.raises(CLIError, match="denied"):
            callback.wait(2)
        thread.join(2)
    assert callback._server is None
    assert responses[0][0] == 403

    with LoopbackCallback(STATE) as callback:
        with pytest.raises(CLIError, match="timed out"):
            callback.wait(0.01)
    assert callback._server is None


@pytest.mark.parametrize(
    "target",
    [
        f"{native.CALLBACK_PATH}?code=unused&state=%C3%A9",
        f"{native.CALLBACK_PATH}?code=unused&state=",
        f"{native.CALLBACK_PATH}?code=unused&state={'a' * 513}",
    ],
)
def test_loopback_rejects_invalid_state_shapes_without_traceback_or_leaks(target: str) -> None:
    responses: list[tuple[int, str]] = []
    thread = threading.Thread(target=_request_loopback, args=(target, responses))
    with LoopbackCallback(STATE) as callback:
        server = callback._server
        thread.start()
        with pytest.raises(CLIError, match="state"):
            callback.wait(2)
        thread.join(2)
        assert callback._watchdog is None
    assert callback._server is None
    assert not thread.is_alive()
    assert server is not None and server.callback_code is None
    assert responses[0][0] in (400, 403)
    assert "TypeError" not in responses[0][1]


def test_unicode_expected_state_is_rejected_without_compare_digest_type_error() -> None:
    with pytest.raises(native._CallbackRejected, match="state"):
        native.parse_callback_target(
            f"{native.CALLBACK_PATH}?code=unused&state={STATE}",
            "nonascii-é",
        )


def test_callback_absolute_deadline_closes_dribbling_connection_and_rejects_late_code() -> None:
    # Repeat the short-deadline race: byte intervals stay below the two-second
    # socket inactivity timeout, so only the absolute watchdog can stop it.
    for _attempt in range(2):
        stop = threading.Event()
        connected = threading.Event()

        def dribble_headers(stop: threading.Event = stop, connected: threading.Event = connected) -> None:
            try:
                with socket.create_connection(
                    (native.LOOPBACK_HOST, native.LOOPBACK_PORT), timeout=1
                ) as connection:
                    connection.sendall(
                        (
                            f"GET {native.CALLBACK_PATH}?code=late-code&state={STATE} "
                            "HTTP/1.1\r\nX-Unfinished: "
                        ).encode("ascii")
                    )
                    connected.set()
                    while not stop.is_set():
                        connection.sendall(b"x")
                        stop.wait(0.02)  # Sleep below the inactivity timeout.
            except OSError:
                # The deadline watchdog deliberately forcibly closes this peer.
                connected.set()

        thread = threading.Thread(target=dribble_headers)
        with LoopbackCallback(STATE) as callback:
            server = callback._server
            started = time.monotonic()
            thread.start()
            assert connected.wait(1)
            with pytest.raises(CLIError, match="timed out"):
                callback.wait(0.15)
            assert time.monotonic() - started < 1.0
            assert callback._watchdog is None
        stop.set()
        thread.join(2)
        assert not thread.is_alive()
        assert callback._server is None
        assert server is not None and server.callback_code is None

        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex((native.LOOPBACK_HOST, native.LOOPBACK_PORT)) != 0


def test_real_loopback_ignores_wrong_path_then_accepts_one_response() -> None:
    responses: list[tuple[int, str]] = []

    def two_requests() -> None:
        _request_loopback("/not-the-callback?code=untrusted", responses)
        _request_loopback(f"{native.CALLBACK_PATH}?code=valid-code&state={STATE}", responses)

    thread = threading.Thread(target=two_requests)
    with LoopbackCallback(STATE) as callback:
        thread.start()
        assert callback.wait(2) == "valid-code"
        thread.join(2)
    assert [status for status, _body in responses] == [404, 200]


def _login_transport(
    requests: list[httpx.Request],
    *,
    refreshed: bool = False,
    include_rotated_refresh: bool = True,
    manual: bool = False,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/cli-config":
            assert request.headers.get("authorization") is None
            return httpx.Response(200, json=DISCOVERY)
        if request.url == TOKEN_ENDPOINT:
            form = parse_qs(request.content.decode("utf-8"), strict_parsing=True)
            assert form["client_id"] == [CLIENT_ID]
            if refreshed:
                assert form["grant_type"] == ["refresh_token"]
                assert form["refresh_token"] == ["old-refresh-credential"]
                assert set(form) == {"grant_type", "refresh_token", "client_id"}
            else:
                assert form == {
                    "grant_type": ["authorization_code"],
                    "code": ["one-time-code"],
                    "client_id": [CLIENT_ID],
                    "redirect_uri": [native.CODE_DISPLAY_REDIRECT_URI if manual else native.REDIRECT_URI],
                    "code_verifier": [VERIFIER],
                }
            body: dict[str, Any] = {
                "access_token": "short-lived-access-token",
                "token_type": "Bearer",
                "expires_in": 600,
            }
            if not refreshed:
                body["refresh_token"] = "new-refresh-credential"
            elif include_rotated_refresh:
                body["refresh_token"] = "rotated-refresh-credential"
            return httpx.Response(200, json=body)
        if request.url.path == "/api/v1/auth/cli/token":
            assert request.method == "POST"
            assert request.headers["authorization"] == "Bearer short-lived-access-token"
            assert json.loads(request.content) == {"name": native.CLI_TOKEN_LABEL}
            return httpx.Response(201, json={"token": "ebs_new-personal-token"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    return httpx.MockTransport(handler)


def test_browser_login_invokes_browser_exchanges_directly_and_returns_no_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    callback = FakeCallback(STATE)
    opened: list[str] = []
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))
    monkeypatch.setattr(
        native.getpass,
        "getpass",
        lambda _message: pytest.fail("local login must not use the manual code prompt"),
    )

    result = native.browser_login(
        "https://ebs.test",
        transport=_login_transport(requests),
        callback_factory=lambda state: callback,
        open_browser=lambda url: opened.append(url) or True,
        wait_seconds=1,
    )

    assert result == LoginCredentials("ebs_new-personal-token", "new-refresh-credential", CLIENT_ID)
    assert callback.entered and callback.closed
    assert callback.expected_state == STATE
    assert len(opened) == 1
    assert VERIFIER not in opened[0]
    assert [request.url.path for request in requests] == [
        "/api/v1/auth/cli-config",
        "/v2/oauth2/token",
        "/api/v1/auth/cli/token",
    ]
    assert "short-lived-access-token" not in repr(result)
    assert "ebs_new-personal-token" not in repr(result)
    assert "new-refresh-credential" not in repr(result)


def test_no_browser_uses_hidden_manual_prompt_without_listener_or_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    printed: list[str] = []
    prompted: list[str] = []
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))

    def hidden_prompt(message: str) -> str:
        prompted.append(message)
        return "  one-time-code\n"

    monkeypatch.setattr(native.getpass, "getpass", hidden_prompt)
    result = native.browser_login(
        "https://ebs.test",
        no_browser=True,
        show_url=printed.append,
        transport=_login_transport(requests, manual=True),
        callback_factory=lambda _state: pytest.fail("manual login must not open a callback listener"),
        open_browser=lambda _url: pytest.fail("browser should not open"),
        wait_seconds=1,
    )

    assert result == LoginCredentials("ebs_new-personal-token", "new-refresh-credential", CLIENT_ID)
    assert len(printed) == 1
    query = parse_qs(urlsplit(printed[0]).query)
    assert query["state"] == [STATE]
    assert query["code_challenge"] == [CHALLENGE]
    assert query["redirect_uri"] == [native.CODE_DISPLAY_REDIRECT_URI]
    assert VERIFIER not in printed[0]
    assert "one-time-code" not in printed[0]
    assert "short-lived-access-token" not in printed[0]
    assert "new-refresh-credential" not in printed[0]
    assert len(prompted) == 1
    assert "input is hidden" in prompted[0]
    assert [request.url.path for request in requests] == [
        "/api/v1/auth/cli-config",
        "/v2/oauth2/token",
        "/api/v1/auth/cli/token",
    ]


@pytest.mark.parametrize("exception_type", [EOFError, KeyboardInterrupt])
def test_manual_login_cancellation_never_exchanges_or_opens_listener(
    exception_type: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))

    def cancel(_message: str) -> str:
        raise exception_type

    with pytest.raises(CLIError, match="cancelled") as caught:
        native.browser_login(
            "https://ebs.test",
            no_browser=True,
            prompt_code=cancel,
            transport=_login_transport(requests, manual=True),
            callback_factory=lambda _state: pytest.fail("manual login must not open a listener"),
            open_browser=lambda _url: pytest.fail("browser should not open"),
        )

    assert [request.url.path for request in requests] == ["/api/v1/auth/cli-config"]
    assert "Nothing was saved" in str(caught.value)


@pytest.mark.parametrize(
    ("supplied", "message"),
    [
        ("", "No authorization code"),
        ("   \n", "No authorization code"),
        ("private-code\nsecond-line", "malformed"),
        ("private-code\x00", "malformed"),
        ("private-codeé", "malformed"),
        ("x" * 4097, "malformed"),
    ],
)
def test_manual_login_rejects_empty_or_malformed_code_before_exchange(
    supplied: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))

    with pytest.raises(CLIError, match=message) as caught:
        native.browser_login(
            "https://ebs.test",
            no_browser=True,
            prompt_code=lambda _message: supplied,
            transport=_login_transport(requests, manual=True),
            callback_factory=lambda _state: pytest.fail("manual login must not open a listener"),
        )

    assert [request.url.path for request in requests] == ["/api/v1/auth/cli-config"]
    if supplied.strip():
        assert supplied.strip() not in str(caught.value)
    assert "Nothing was saved" in str(caught.value)


def test_manual_bad_code_and_exchange_body_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    printed: list[str] = []
    secret = "one-time-code-and-provider-response-must-not-leak"
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("cli-config"):
            return httpx.Response(200, json=DISCOVERY)
        if request.url == TOKEN_ENDPOINT:
            return httpx.Response(400, text=secret)
        return httpx.Response(201, json={"token": "ebs_must-not-be-created"})

    with pytest.raises(CLIError, match="rejected.*authorization") as caught:
        native.browser_login(
            "https://ebs.test",
            no_browser=True,
            show_url=printed.append,
            prompt_code=lambda _message: secret,
            transport=httpx.MockTransport(handler),
            callback_factory=lambda _state: pytest.fail("manual login must not open a listener"),
        )

    assert [request.url.path for request in requests] == [
        "/api/v1/auth/cli-config",
        "/v2/oauth2/token",
    ]
    assert secret not in str(caught.value)
    assert secret not in "".join(printed)
    assert "Nothing was saved" in str(caught.value)


def test_browser_failure_closes_callback_before_any_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def discovery(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=DISCOVERY)

    callback = FakeCallback(STATE)
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))
    with pytest.raises(CLIError, match="Could not open"):
        native.browser_login(
            "https://ebs.test",
            transport=httpx.MockTransport(discovery),
            callback_factory=lambda state: callback,
            open_browser=lambda _url: False,
        )
    assert callback.closed
    assert calls == ["/api/v1/auth/cli-config"]


def test_missing_native_configuration_is_unavailable() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            503,
            json={"detail": "CLI authentication is unavailable", "secret": "must-not-leak"},
        )
    )
    with pytest.raises(CLIError) as caught:
        native.discover_native_client("https://ebs.test", transport=transport)
    assert "Browser login is unavailable" in str(caught.value)
    assert "must-not-leak" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {"refresh_token": "refresh-credential"},
        {"access_token": "access-credential"},
        {"access_token": "access-credential", "refresh_token": 123},
        {"access_token": "access-credential", "refresh_token": "refresh", "expires_in": "600"},
        {"access_token": "access-credential", "refresh_token": "refresh", "token_type": "basic"},
    ],
)
def test_strict_initial_oauth_response_validation(
    body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    callback = FakeCallback(STATE)
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("cli-config"):
            return httpx.Response(200, json=DISCOVERY)
        return httpx.Response(200, json={**body, "echoed_secret": "sensitive-response-content"})

    with pytest.raises(CLIError, match="invalid code exchange") as caught:
        native.browser_login(
            "https://ebs.test",
            transport=httpx.MockTransport(handler),
            callback_factory=lambda state: callback,
            open_browser=lambda _url: True,
        )
    assert "sensitive-response-content" not in str(caught.value)
    assert callback.closed


def test_oauth_error_body_and_oversized_body_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = FakeCallback(STATE)
    monkeypatch.setattr(native, "generate_oauth_material", lambda: (STATE, VERIFIER, CHALLENGE))
    secret = "authorization-secret-in-error-body"
    response_bodies = iter(
        (
            httpx.Response(400, text=secret),
            httpx.Response(200, content=b"x" * (native.MAX_RESPONSE_BYTES + 1)),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("cli-config"):
            return httpx.Response(200, json=DISCOVERY)
        return next(response_bodies)

    for message in ("rejected the browser authorization", "oversized response"):
        with pytest.raises(CLIError, match=message) as caught:
            native.browser_login(
                "https://ebs.test",
                transport=httpx.MockTransport(handler),
                callback_factory=lambda state: callback,
                open_browser=lambda _url: True,
            )
        assert secret not in str(caught.value)


def test_provider_and_token_creation_require_https_outside_injected_transports() -> None:
    with pytest.raises(CLIError, match="must use HTTPS"):
        native.discover_native_client("http://ebs.test")
    with pytest.raises(CLIError, match="must use HTTPS"):
        native.authorization_url(
            NativeClientConfig(CLIENT_ID, "http://auth.example/authorize", TOKEN_ENDPOINT),
            state=STATE,
            challenge=CHALLENGE,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/v2/oauth2/token",
        "https://169.254.169.254/v2/oauth2/token",
        "https://[::1]/v2/oauth2/token",
        "https://auth.globus.org:8443/v2/oauth2/token",
        "https://auth.globus.org/v2/oauth2/token/alternate",
        "https://auth.globus.org.evil.example/v2/oauth2/token",
        "https://auth.globus.org@evil.example/v2/oauth2/token",
        "https://auth.globus.org./v2/oauth2/token",
        "http://auth.globus.org/v2/oauth2/token",
    ],
)
def test_real_discovery_rejects_noncanonical_provider_hosts_ports_and_paths(
    endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def discovery_only(*args: Any, **kwargs: Any) -> native._Response:
        calls.append(args[1])
        return native._Response(
            200,
            "application/json",
            json.dumps(
                {
                    **DISCOVERY,
                    "authorization_endpoint": native.GLOBUS_AUTHORIZATION_ENDPOINT,
                    "token_endpoint": endpoint,
                }
            ).encode(),
        )

    monkeypatch.setattr(native, "_request", discovery_only)
    with pytest.raises(CLIError, match="unsafe provider endpoints") as caught:
        native.discover_native_client("https://ebs.test", transport=None)
    assert calls == ["https://ebs.test/api/v1/auth/cli-config"]
    assert endpoint not in str(caught.value)


def test_real_normalized_canonical_endpoints_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        native,
        "_request",
        lambda *_args, **_kwargs: native._Response(
            200,
            "application/json",
            json.dumps(
                {
                    **DISCOVERY,
                    "authorization_endpoint": "https://AUTH.GLOBUS.ORG:443/v2/oauth2/authorize",
                    "token_endpoint": "https://auth.globus.org:443/v2/oauth2/token",
                }
            ).encode(),
        ),
    )

    config = native.discover_native_client("https://ebs.test", transport=None)

    assert config.token_endpoint == "https://auth.globus.org:443/v2/oauth2/token"


def test_refresh_rejects_unsafe_config_before_constructing_credential_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malicious = "https://169.254.169.254/token?refresh=stolen-marker"
    grant = "old-refresh-credential"
    monkeypatch.setattr(
        native,
        "discover_native_client",
        lambda *_args, **_kwargs: NativeClientConfig(
            CLIENT_ID,
            native.GLOBUS_AUTHORIZATION_ENDPOINT,
            malicious,
        ),
    )
    requests: list[str] = []

    def forbidden_request(*_args: Any, **_kwargs: Any) -> native._Response:
        requests.append("credential-bearing request was constructed")
        raise AssertionError("a refresh request must not be constructed")

    monkeypatch.setattr(native, "_request", forbidden_request)
    with pytest.raises(CLIError, match="unsupported provider endpoints") as caught:
        native.refresh_login("https://ebs.test", grant, CLIENT_ID, transport=None)
    assert requests == []
    assert malicious not in str(caught.value)
    assert grant not in str(caught.value)


def test_real_browser_flow_validates_config_before_generating_or_opening_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        native,
        "discover_native_client",
        lambda *_args, **_kwargs: NativeClientConfig(
            CLIENT_ID,
            "https://127.0.0.1/authorize",
            native.GLOBUS_TOKEN_ENDPOINT,
        ),
    )
    monkeypatch.setattr(
        native,
        "generate_oauth_material",
        lambda: pytest.fail("OAuth material must not be generated for an unsafe provider"),
    )
    with pytest.raises(CLIError, match="unsupported provider endpoints"):
        native.browser_login(
            "https://ebs.test",
            transport=None,
            callback_factory=lambda _state: pytest.fail("an unsafe provider must not open a callback"),
            open_browser=lambda _url: pytest.fail("an unsafe provider must not open a browser"),
        )


def test_provider_redirect_is_rejected_without_following_or_disclosing_grant() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("cli-config"):
            return httpx.Response(200, json=DISCOVERY)
        return httpx.Response(
            302,
            headers={"location": "https://169.254.169.254/stolen"},
            text="old-refresh-credential",
        )

    with pytest.raises(CLIError, match="unavailable") as caught:
        native.refresh_login(
            "https://ebs.test",
            "old-refresh-credential",
            CLIENT_ID,
            transport=httpx.MockTransport(handler),
        )

    assert len(requests) == 2
    assert all(request.url.host in {"ebs.test", "auth.globus.test"} for request in requests)
    assert "169.254.169.254" not in str(caught.value)
    assert "old-refresh-credential" not in str(caught.value)


def test_refresh_rotation_and_refresh_reuse() -> None:
    for rotated, expected in ((True, "rotated-refresh-credential"), (False, "old-refresh-credential")):
        requests: list[httpx.Request] = []
        result = native.refresh_login(
            "https://ebs.test",
            "old-refresh-credential",
            CLIENT_ID,
            transport=_login_transport(requests, refreshed=True, include_rotated_refresh=rotated),
        )
        assert result.token == "ebs_new-personal-token"
        assert result.refresh_token == expected
        assert result.client_id == CLIENT_ID


def test_refresh_rejection_does_not_mint_or_modify_saved_credentials(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_previous-token", "old-refresh-credential", CLIENT_ID, path)
    before = path.read_bytes()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("cli-config"):
            return httpx.Response(200, json=DISCOVERY)
        assert request.url == TOKEN_ENDPOINT
        return httpx.Response(400, text="old-refresh-credential")

    with pytest.raises(CLIError, match="left unchanged") as caught:
        native.refresh_login(
            "https://ebs.test",
            "old-refresh-credential",
            CLIENT_ID,
            transport=httpx.MockTransport(handler),
        )
    assert "old-refresh-credential" not in str(caught.value)
    assert calls == ["/api/v1/auth/cli-config", "/v2/oauth2/token"]
    assert path.read_bytes() == before


def test_refresh_rejects_a_changed_native_client_without_sending_grant() -> None:
    with pytest.raises(CLIError, match="configuration changed"):
        native.refresh_login(
            "https://ebs.test",
            "old-refresh-credential",
            "different-client",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=DISCOVERY)),
        )


def test_revocation_uses_public_client_without_web_secret() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, text="active=false")

    native.revoke_refresh_token(
        "logout-refresh-credential",
        CLIENT_ID,
        transport=httpx.MockTransport(handler),
    )
    assert seen == {
        "url": native.REVOCATION_ENDPOINT,
        "authorization": None,
        "form": {"token": ["logout-refresh-credential"], "client_id": [CLIENT_ID]},
    }


def test_cli_browser_login_saves_both_credentials_and_project(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    save_project("physics", path)
    monkeypatch.setattr(
        "ebs_cli.cli.native_browser_login",
        lambda *_args, **_kwargs: LoginCredentials("ebs_browser-token", "browser-refresh", CLIENT_ID),
    )

    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 0, result.output
    body = read_config(path)
    assert body == {
        "project": "physics",
        "token": "ebs_browser-token",
        GLOBUS_REFRESH_TOKEN_KEY: "browser-refresh",
        GLOBUS_CLIENT_ID_KEY: CLIENT_ID,
    }
    assert "ebs_browser-token" not in result.output
    assert "browser-refresh" not in result.output
    assert str(path) not in result.output


def test_cli_manual_login_prints_instructions_without_secrets_or_config_path(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    seen: dict[str, Any] = {}
    manual_url = (
        "https://auth.globus.test/v2/oauth2/authorize?"
        "state=public-test-state&code_challenge=public-test-challenge&"
        "redirect_uri=https%3A%2F%2Fauth.globus.org%2Fv2%2Fweb%2Fauth-code"
    )

    def manual_login(*_args: Any, **kwargs: Any) -> LoginCredentials:
        seen.update(kwargs)
        kwargs["show_url"](manual_url)
        return LoginCredentials("ebs_manual-token", "manual-refresh", CLIENT_ID)

    monkeypatch.setattr(cli_module, "native_browser_login", manual_login)

    result = runner.invoke(app, ["auth", "login", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert seen["no_browser"] is True
    assert manual_url in result.stdout
    assert "any device" in result.stdout
    assert "copy the one-time code" in result.stdout
    assert "ebs_manual-token" not in result.output
    assert "manual-refresh" not in result.output
    assert str(path) not in result.output
    assert read_config(path)["token"] == "ebs_manual-token"


def test_cli_refresh_replaces_personal_token_and_rotated_grant(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_previous-token", "previous-refresh", CLIENT_ID, path)
    save_project("physics", path)
    monkeypatch.setattr(
        "ebs_cli.cli.native_refresh_login",
        lambda *_args: LoginCredentials("ebs_rotated-token", "rotated-refresh", CLIENT_ID),
    )
    monkeypatch.setenv("EBS_TOKEN", "ebs_environment-does-not-change-refresh")

    result = runner.invoke(app, ["auth", "refresh"])

    assert result.exit_code == 0, result.output
    assert load_token(path) == "ebs_rotated-token"
    assert read_config(path)[GLOBUS_REFRESH_TOKEN_KEY] == "rotated-refresh"
    assert read_config(path)["project"] == "physics"
    assert "rotated-refresh" not in result.output
    assert str(path) not in result.output


def test_cli_refresh_failure_preserves_saved_credentials(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_previous-token", "previous-refresh", CLIENT_ID, path)
    before = path.read_bytes()
    monkeypatch.setattr(
        "ebs_cli.cli.native_refresh_login",
        lambda *_args: (_ for _ in ()).throw(CLIError("Saved Globus login rejected. Login again.")),
    )

    result = runner.invoke(app, ["auth", "refresh"])

    assert result.exit_code != 0
    assert path.read_bytes() == before


def test_refresh_fails_without_saving_when_local_logout_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_previous-token", "previous-refresh", CLIENT_ID, path)
    save_project("physics", path)
    body = read_config(path)
    body["custom"] = {"keep": True}
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")
    path.chmod(0o600)

    refresh_started = threading.Event()
    finish_refresh = threading.Event()
    errors: list[BaseException] = []

    def in_flight_refresh(*_args: Any) -> LoginCredentials:
        refresh_started.set()
        if not finish_refresh.wait(2):
            raise AssertionError("test did not release the in-flight provider refresh")
        return LoginCredentials("ebs_refreshed-token", "rotated-refresh", CLIENT_ID)

    monkeypatch.setattr(cli_module, "native_refresh_login", in_flight_refresh)
    context = SimpleNamespace(ensure_object=lambda _object_type: cli_module.Options("https://ebs.test", None))

    def run_refresh_command() -> None:
        try:
            cli_module.refresh.__wrapped__(context)
        except BaseException as exc:
            errors.append(exc)

    refresh_thread = threading.Thread(target=run_refresh_command, name="refresh-command")
    refresh_thread.start()
    assert refresh_started.wait(1)
    try:
        # This is the real command body with --local-only=True, so no provider
        # call is involved. It must finish while refresh is still in flight.
        cli_module.logout.__wrapped__(local_only=True)
        assert optional_globus_credentials(path) is None
        assert read_config(path) == {"project": "physics", "custom": {"keep": True}}
    finally:
        finish_refresh.set()
    refresh_thread.join(2)

    assert not refresh_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], CLIError)
    assert "Concurrent login state changed" in str(errors[0])
    assert "rotated-refresh" not in str(errors[0])
    assert read_config(path) == {"project": "physics", "custom": {"keep": True}}
    assert optional_globus_credentials(path) is None


def test_logout_does_not_remove_or_claim_revocation_for_a_newer_login(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_previous-token", "previous-refresh", CLIENT_ID, path)
    save_project("physics", path)
    body = read_config(path)
    body["custom"] = {"keep": True}
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")
    path.chmod(0o600)

    def revoke_then_interleave_login(refresh_token: str, client_id: str) -> None:
        assert (refresh_token, client_id) == ("previous-refresh", CLIENT_ID)
        # Model a separate login committing after provider revocation succeeds
        # but before logout acquires the config transaction lock to clear it.
        save_auth_credentials("ebs_other-login", "other-refresh", CLIENT_ID, path)

    monkeypatch.setattr(cli_module, "revoke_refresh_token", revoke_then_interleave_login)

    result = runner.invoke(app, ["auth", "logout"])

    assert result.exit_code != 0
    assert "Concurrent login state changed" in result.output
    assert "confirmed revocation" not in result.output
    assert "Removed the saved" not in result.output
    assert "other-refresh" not in result.output
    assert load_token(path) == "ebs_other-login"
    assert load_globus_credentials(path) == ("other-refresh", CLIENT_ID)
    assert read_config(path)["project"] == "physics"
    assert read_config(path)["custom"] == {"keep": True}


def test_cli_logout_revocation_success_failure_and_local_only(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"

    monkeypatch.setattr("ebs_cli.cli.revoke_refresh_token", lambda *_args: None)
    save_auth_credentials("ebs_saved-token", "saved-refresh", CLIENT_ID, path)
    save_project("physics", path)
    result = runner.invoke(app, ["auth", "logout"])
    assert result.exit_code == 0
    assert "confirmed revocation" in result.stdout
    assert read_config(path) == {"project": "physics"}

    def fail_revocation(*_args: Any) -> None:
        raise CLIError("Globus revocation timed out; saved-refresh must not appear")

    monkeypatch.setattr("ebs_cli.cli.revoke_refresh_token", fail_revocation)
    save_auth_credentials("ebs_saved-token", "saved-refresh", CLIENT_ID, path)
    result = runner.invoke(app, ["auth", "logout"])
    assert result.exit_code == 0
    assert "was not confirmed" in result.stdout
    assert "confirmed revocation" not in result.stdout
    assert "saved-refresh" not in result.output
    assert read_config(path) == {"project": "physics"}

    monkeypatch.setattr(
        "ebs_cli.cli.revoke_refresh_token",
        lambda *_args: pytest.fail("--local-only must not contact Globus"),
    )
    save_auth_credentials("ebs_saved-token", "saved-refresh", CLIENT_ID, path)
    result = runner.invoke(app, ["auth", "logout", "--local-only"])
    assert result.exit_code == 0
    assert "did not revoke" in result.stdout
    assert read_config(path) == {"project": "physics"}
    assert logout(path) is False


def test_manual_token_switch_rejects_token_arguments_without_saving(
    runner: CliRunner, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    supplied = "ebs_command-lines-must-never-contain-this"

    result = runner.invoke(app, ["auth", "login", "--token", supplied])

    assert result.exit_code != 0
    assert not path.exists()
    assert supplied not in result.output
