import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from conftest import TRIGGER_ID, trigger_data

from ebs_cli import cli
from ebs_cli.client import EBSClient
from ebs_cli.config import (
    config_path,
    load_globus_credentials,
    load_token,
    read_config,
    save_auth_credentials,
)
from ebs_cli.errors import CLIError
from ebs_cli.native_auth import LoginCredentials

OLD_TOKEN = "ebs_expired-saved-token"
NEW_TOKEN = "ebs_replacement-saved-token"
OLD_REFRESH = "old-refresh-credential"
NEW_REFRESH = "rotated-refresh-credential"
CLIENT_ID = "native-client-test"
OTHER_TOKEN = "ebs_concurrent-replacement-token"
OTHER_REFRESH = "concurrent-refresh-credential"


def projects_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"items": [{"name": "physics", "role": "maintainer", "trigger_count": 0}]},
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    renew_token: Callable[[], str] | None = None,
) -> EBSClient:
    return EBSClient(
        "https://ebs.test",
        OLD_TOKEN,
        "physics",
        transport=httpx.MockTransport(handler),
        renew_token=renew_token,
    )


def make_saved_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    project: str | None = "physics",
) -> EBSClient:
    """Inject only the API transport; use the real saved-auth composition path."""
    monkeypatch.delenv("EBS_TOKEN")
    real_client = EBSClient

    def client_factory(
        url: str,
        token: str,
        selected_project: str | None,
        *,
        renew_token: Callable[[], str] | None = None,
    ) -> EBSClient:
        return real_client(
            url,
            token,
            selected_project,
            transport=httpx.MockTransport(handler),
            renew_token=renew_token,
        )

    monkeypatch.setattr(cli, "EBSClient", client_factory)
    return cli._normal_api_client(cli.Options("https://ebs.test", None), project)


def test_saved_login_401_refreshes_atomically_updates_bearer_and_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_path()
    save_auth_credentials(OLD_TOKEN, OLD_REFRESH, CLIENT_ID, path)
    body = read_config(path)
    body["project"] = "physics"
    body["custom"] = {"keep": True}
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")
    path.chmod(0o600)
    requests: list[httpx.Request] = []
    refresh_calls: list[tuple[str, str, str]] = []

    def refresh(url: str, refresh_token: str, client_id: str) -> LoginCredentials:
        refresh_calls.append((url, refresh_token, client_id))
        return LoginCredentials(NEW_TOKEN, NEW_REFRESH, CLIENT_ID)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            assert request.headers["authorization"] == f"Bearer {OLD_TOKEN}"
            return httpx.Response(401, json={"detail": OLD_TOKEN})
        assert request.headers["authorization"] == f"Bearer {NEW_TOKEN}"
        return projects_response()

    monkeypatch.setattr(cli, "native_refresh_login", refresh)
    with make_saved_client(monkeypatch, handler) as client:
        projects = client.list_projects(include_trigger_counts=True)

    assert [project.name for project in projects] == ["physics"]
    assert len(requests) == 2
    assert all(dict(request.url.params) == {"include_trigger_counts": "true"} for request in requests)
    assert refresh_calls == [("https://ebs.test", OLD_REFRESH, CLIENT_ID)]
    assert load_token(path) == NEW_TOKEN
    assert load_globus_credentials(path) == (NEW_REFRESH, CLIENT_ID)
    assert read_config(path)["custom"] == {"keep": True}
    assert read_config(path)["project"] == "physics"


def test_second_401_fails_without_another_refresh_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_path()
    save_auth_credentials(OLD_TOKEN, OLD_REFRESH, CLIENT_ID, path)
    requests: list[httpx.Request] = []
    refresh_calls = 0

    def refresh(*_args: Any) -> LoginCredentials:
        nonlocal refresh_calls
        refresh_calls += 1
        return LoginCredentials(NEW_TOKEN, NEW_REFRESH, CLIENT_ID)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"detail": f"rejected {OLD_TOKEN} {NEW_TOKEN}"})

    monkeypatch.setattr(cli, "native_refresh_login", refresh)
    with make_saved_client(monkeypatch, handler) as client:
        with pytest.raises(CLIError, match="Authentication failed") as caught:
            client.list_projects()

    assert len(requests) == 2
    assert refresh_calls == 1
    assert requests[0].headers["authorization"] == f"Bearer {OLD_TOKEN}"
    assert requests[1].headers["authorization"] == f"Bearer {NEW_TOKEN}"
    assert load_token(path) == NEW_TOKEN
    assert load_globus_credentials(path) == (NEW_REFRESH, CLIENT_ID)
    for secret in (OLD_TOKEN, NEW_TOKEN, OLD_REFRESH, NEW_REFRESH):
        assert secret not in str(caught.value)


def test_manual_token_file_never_borrows_an_old_globus_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_path()
    save_auth_credentials(OLD_TOKEN, OLD_REFRESH, CLIENT_ID, path)
    save_auth_credentials("ebs_intermediate-browser-token", OLD_REFRESH, CLIENT_ID, path)
    cli.save_token(OLD_TOKEN, path)
    before = path.read_bytes()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, json={"detail": "expired"})

    monkeypatch.setattr(
        cli,
        "native_refresh_login",
        lambda *_args: pytest.fail("manual-token login must not renew through Globus"),
    )
    with make_saved_client(monkeypatch, handler) as client:
        with pytest.raises(CLIError, match="Authentication failed"):
            client.list_projects()

    assert requests == 1
    assert path.read_bytes() == before
    assert "globus_refresh_token" not in read_config(path)
    assert "globus_client_id" not in read_config(path)


def test_environment_token_never_borrows_saved_file_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_path()
    save_auth_credentials(OLD_TOKEN, OLD_REFRESH, CLIENT_ID, path)
    before = path.read_bytes()
    environment_token = "ebs_environment-only-token"
    requests: list[httpx.Request] = []
    real_client = EBSClient

    def client_factory(
        url: str,
        token: str,
        project: str | None,
        *,
        renew_token: Callable[[], str] | None = None,
    ) -> EBSClient:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(401, json={"detail": "expired"})

        return real_client(
            url,
            token,
            project,
            transport=httpx.MockTransport(handler),
            renew_token=renew_token,
        )

    monkeypatch.setattr(cli, "EBSClient", client_factory)
    monkeypatch.setenv("EBS_TOKEN", environment_token)
    monkeypatch.setattr(
        cli,
        "native_refresh_login",
        lambda *_args: pytest.fail("EBS_TOKEN must not trigger saved-grant renewal"),
    )

    with cli._normal_api_client(cli.Options("https://ebs.test", None), None) as client:
        with pytest.raises(CLIError, match="Authentication failed"):
            client.list_projects()

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {environment_token}"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "globus_fields",
    [
        {},
        {"globus_refresh_token": OLD_REFRESH},
        {"globus_client_id": CLIENT_ID},
        {"globus_refresh_token": "bad\nrefresh", "globus_client_id": CLIENT_ID},
        {"globus_refresh_token": OLD_REFRESH, "globus_client_id": ""},
    ],
)
def test_absent_incomplete_or_invalid_grant_keeps_normal_401_guidance(
    monkeypatch: pytest.MonkeyPatch, globus_fields: dict[str, str]
) -> None:
    path = config_path()
    path.write_text(json.dumps({"token": OLD_TOKEN, **globus_fields}) + "\n", encoding="utf-8")
    path.chmod(0o600)
    before = path.read_bytes()
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, json={"detail": "expired"})

    monkeypatch.setattr(
        cli,
        "native_refresh_login",
        lambda *_args: pytest.fail("only a complete valid saved grant may refresh"),
    )
    with make_saved_client(monkeypatch, handler) as client:
        with pytest.raises(CLIError, match="Authentication failed"):
            client.list_projects()

    assert requests == 1
    assert path.read_bytes() == before


def test_file_changed_before_refresh_never_spends_grant_or_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_path()
    save_auth_credentials(OLD_TOKEN, OLD_REFRESH, CLIENT_ID, path)
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, json={"detail": OLD_REFRESH})

    monkeypatch.setattr(
        cli,
        "native_refresh_login",
        lambda *_args: pytest.fail("changed files must fail before provider refresh"),
    )
    client = make_saved_client(monkeypatch, handler)
    save_auth_credentials(OTHER_TOKEN, OTHER_REFRESH, CLIENT_ID, path)
    before = path.read_bytes()
    with client:
        with pytest.raises(CLIError, match="Concurrent login state changed") as caught:
            client.list_projects()

    assert requests == 1
    assert path.read_bytes() == before
    assert load_token(path) == OTHER_TOKEN
    assert OTHER_REFRESH not in str(caught.value)
    assert OLD_REFRESH not in str(caught.value)


def test_conflict_after_provider_refresh_prevents_save_and_api_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_path()
    save_auth_credentials(OLD_TOKEN, OLD_REFRESH, CLIENT_ID, path)
    requests = 0

    def refresh(*_args: Any) -> LoginCredentials:
        # Model another login committing while Globus renewal is in flight.
        save_auth_credentials(OTHER_TOKEN, OTHER_REFRESH, CLIENT_ID, path)
        return LoginCredentials(NEW_TOKEN, NEW_REFRESH, CLIENT_ID)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, json={"detail": "expired"})

    monkeypatch.setattr(cli, "native_refresh_login", refresh)
    with make_saved_client(monkeypatch, handler) as client:
        with pytest.raises(CLIError, match="Concurrent login state changed") as caught:
            client.list_projects()

    assert requests == 1
    assert load_token(path) == OTHER_TOKEN
    assert load_globus_credentials(path) == (OTHER_REFRESH, CLIENT_ID)
    for secret in (OLD_TOKEN, OLD_REFRESH, NEW_TOKEN, NEW_REFRESH, OTHER_TOKEN, OTHER_REFRESH):
        assert secret not in str(caught.value)


def test_globus_rejection_never_retries_api_or_changes_saved_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = config_path()
    save_auth_credentials(OLD_TOKEN, OLD_REFRESH, CLIENT_ID, path)
    before = path.read_bytes()
    requests = 0

    def reject(*_args: Any) -> LoginCredentials:
        raise CLIError("Saved Globus login was rejected. Nothing was saved.")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401, json={"detail": "expired"})

    monkeypatch.setattr(cli, "native_refresh_login", reject)
    with make_saved_client(monkeypatch, handler) as client:
        with pytest.raises(CLIError, match="rejected") as caught:
            client.list_projects()

    assert requests == 1
    assert path.read_bytes() == before
    assert OLD_REFRESH not in str(caught.value)
    assert NEW_TOKEN not in str(caught.value)


@pytest.mark.parametrize("status", [403, 404, 500, 307])
def test_other_http_statuses_never_refresh_or_retry(status: int) -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, json={"detail": "request denied"})

    with make_client(
        handler,
        renew_token=lambda: pytest.fail("non-401 responses must not refresh"),
    ) as client:
        with pytest.raises(CLIError):
            client.list_projects()

    assert requests == 1


def test_network_error_never_refreshes_or_retries() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ConnectError(OLD_REFRESH, request=request)

    with make_client(
        handler,
        renew_token=lambda: pytest.fail("network errors must not refresh"),
    ) as client:
        with pytest.raises(CLIError) as caught:
            client.list_projects()

    assert requests == 1
    assert OLD_REFRESH not in str(caught.value)


def test_refresh_failure_never_retries_original_request() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(401)

    def reject() -> str:
        raise CLIError("Renewal failed. Nothing was saved.")

    with make_client(handler, renew_token=reject) as client:
        with pytest.raises(CLIError, match="Renewal failed") as caught:
            client.list_projects()

    assert requests == 1
    assert OLD_REFRESH not in str(caught.value)


def test_patch_401_replays_same_bounded_json_exactly_once() -> None:
    requests: list[httpx.Request] = []
    renewals = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=trigger_data(enabled=False))

    def renew() -> str:
        nonlocal renewals
        renewals += 1
        return NEW_TOKEN

    with make_client(handler, renew) as client:
        result = client.set_trigger_enabled(TRIGGER_ID, False)

    assert result.enabled is False
    assert renewals == 1
    assert len(requests) == 2
    assert all(request.method == "PATCH" for request in requests)
    assert [json.loads(request.content) for request in requests] == [{"enabled": False}] * 2
    assert requests[0].headers["authorization"] == f"Bearer {OLD_TOKEN}"
    assert requests[1].headers["authorization"] == f"Bearer {NEW_TOKEN}"


def test_delete_401_replays_empty_request_exactly_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(204)

    with make_client(handler, lambda: NEW_TOKEN) as client:
        assert client.delete_trigger(TRIGGER_ID) is None

    assert len(requests) == 2
    assert all(request.method == "DELETE" and not request.content for request in requests)


def test_credential_creation_post_401_replays_bounded_json_exactly_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "bucket": "ebs-physics",
                "endpoint_url": "https://s3.example.test",
                "region": "us-east-1",
                "access_key_id": "temporary-access",
                "secret_access_key": "temporary-secret",
                "session_token": "temporary-session",
                "expires_at": "2026-11-03T12:15:00Z",
            },
        )

    with make_client(handler, lambda: NEW_TOKEN) as client:
        result = client.s3_credentials(TRIGGER_ID, duration_seconds=900, operation="list")

    assert result.bucket == "ebs-physics"
    assert len(requests) == 2
    expected_body = {"duration_seconds": 900, "operation": "list"}
    assert [json.loads(request.content) for request in requests] == [expected_body] * 2
