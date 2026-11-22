import json

import httpx
import pytest
from conftest import SIGNED_URL, TRIGGER_ID, event_data, trigger_data

from ebs_cli.client import EBSClient, normalize_server_url, validate_object_key
from ebs_cli.errors import CLIError


def make_client(handler, token: str = "ebs_transport-test-token") -> EBSClient:
    return EBSClient("https://ebs.example.test", token, "physics", transport=httpx.MockTransport(handler))


def test_trigger_request_uses_bearer_project_and_offset_parameters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/triggers"
        assert request.url.params["limit"] == "7"
        assert request.url.params["offset"] == "14"
        assert request.url.params["enabled"] == "true"
        assert request.headers["Authorization"] == "Bearer ebs_transport-test-token"
        assert request.headers["X-Group"] == "physics"
        return httpx.Response(200, json={"items": [trigger_data()], "total": 15, "limit": 7, "offset": 14})

    with make_client(handler) as client:
        page = client.list_triggers(limit=7, offset=14, enabled=True)

    assert page.items[0].id == TRIGGER_ID
    assert page.total == 15


def test_trigger_document_write_and_credential_metadata_requests() -> None:
    requests: list[httpx.Request] = []
    create = {
        "name": "data-ready",
        "group": "physics",
        "version": "v3",
        "enabled": True,
        "machine": "perlmutter",
        "sfapi_token_ref": "00000000-0000-7000-8000-00000000c001",
        "sfapi_account": "shared",
        "pipeline": trigger_data()["pipeline"],
    }
    update = {key: value for key, value in create.items() if key != "group"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/me/sfapi-tokens":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json=[
                    {
                        "token_ref": "00000000-0000-7000-8000-00000000c001",
                        "sfapi_username": "alice",
                        "uploader": "owner",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "issued_at": None,
                        "expires_at": "2037-01-01T00:00:00Z",
                        "accounts": ["alice", "c_shared"],
                        "can_manage": True,
                    }
                ],
            )
        if request.method == "POST":
            assert request.url.path == "/api/v1/triggers"
            return httpx.Response(201, json=trigger_data(sfapi_token_ref=create["sfapi_token_ref"]))
        assert request.method == "PATCH"
        assert request.url.path == f"/api/v1/triggers/{TRIGGER_ID}"
        return httpx.Response(200, json=trigger_data(sfapi_token_ref=create["sfapi_token_ref"]))

    with make_client(handler) as client:
        credentials = client.list_sfapi_tokens()
        created = client.create_trigger(create)
        updated = client.update_trigger(TRIGGER_ID, update)

    assert credentials[0].sfapi_username == "alice"
    assert credentials[0].accounts == ["alice", "c_shared"]
    assert created.id == updated.id == TRIGGER_ID
    assert json.loads(requests[1].content) == create
    assert json.loads(requests[2].content) == update


def test_trigger_write_error_never_echoes_complete_authored_document() -> None:
    marker = "complete-private-template-and-request-body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": f"validation failed for {marker} with ebs_echoed-token"},
        )

    with make_client(handler) as client:
        with pytest.raises(CLIError) as caught:
            client.create_trigger({"name": marker})

    assert marker not in str(caught.value)
    assert "ebs_echoed-token" not in str(caught.value)
    assert "secrets-bearing" in str(caught.value)


def test_event_request_uses_cursor_filters_and_typed_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/triggers/{TRIGGER_ID}/events"
        assert request.url.params == httpx.QueryParams(
            {"limit": "2", "cursor": "opaque-cursor", "status": "failed", "source": "s3"}
        )
        return httpx.Response(
            200,
            json={"items": [event_data()], "next_cursor": "later", "has_more": True, "limit": 2},
        )

    with make_client(handler) as client:
        page = client.list_events(TRIGGER_ID, limit=2, cursor="opaque-cursor", status="failed", source="s3")

    assert page.next_cursor == "later"
    assert page.items[0].job is not None


def test_s3_requests_match_endpoint_bodies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/credentials"):
            return httpx.Response(
                200,
                json={
                    "bucket": "ebs-physics",
                    "endpoint_url": "https://s3.example.test",
                    "region": "us-east-1",
                    "access_key_id": "access",
                    "secret_access_key": "secret",
                    "session_token": "session",
                    "expires_at": "2026-11-03T12:15:00Z",
                },
            )
        return httpx.Response(
            200,
            json={
                "bucket": "ebs-physics",
                "key": "folder/data.fits",
                "method": "PUT",
                "url": SIGNED_URL,
                "headers": {},
                "expires_at": "2026-11-03T12:15:00Z",
            },
        )

    with make_client(handler) as client:
        credentials = client.s3_credentials(TRIGGER_ID, duration_seconds=1200)
        upload = client.s3_upload_url(TRIGGER_ID, "folder/data.fits", duration_seconds=1500)

    assert credentials.bucket == "ebs-physics"
    assert upload.method == "PUT"
    assert json.loads(requests[0].content) == {"duration_seconds": 1200}
    assert json.loads(requests[1].content) == {"key": "folder/data.fits", "duration_seconds": 1500}
    assert requests[0].method == requests[1].method == "POST"


def test_s3_credentials_request_sends_operation_when_given() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "bucket": "ebs-physics",
                "endpoint_url": "https://s3.example.test",
                "region": "us-east-1",
                "access_key_id": "access",
                "secret_access_key": "secret",
                "session_token": "session",
                "expires_at": "2026-11-03T12:15:00Z",
                "operation": "list",
            },
        )

    with make_client(handler) as client:
        credentials = client.s3_credentials(TRIGGER_ID, duration_seconds=1200, operation="list")

    assert json.loads(requests[0].content) == {"duration_seconds": 1200, "operation": "list"}
    assert credentials.bucket == "ebs-physics"


def test_prefix_and_cursor_validation_rejects_unsafe_values() -> None:
    from ebs_cli.client import validate_cursor, validate_prefix

    assert validate_prefix("run/2026/") == "run/2026/"
    assert validate_prefix("") == ""
    assert validate_cursor(" opaque-token ") == "opaque-token"
    with pytest.raises(CLIError):
        validate_prefix("bad\nprefix")
    with pytest.raises(CLIError):
        validate_prefix("p" * 1025)
    with pytest.raises(CLIError):
        validate_cursor("")
    with pytest.raises(CLIError):
        validate_cursor("   ")
    with pytest.raises(CLIError):
        validate_cursor("bad\ncursor")


def test_sensitive_error_does_not_echo_credentials_or_signed_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"detail": f"bad ebs_echoed-token {SIGNED_URL} secret=also-sensitive"},
        )

    with make_client(handler) as client:
        with pytest.raises(CLIError) as caught:
            client.s3_credentials(TRIGGER_ID)

    message = str(caught.value)
    assert "ebs_echoed-token" not in message
    assert "signed-secret" not in message
    assert "also-sensitive" not in message
    assert "secrets-bearing" in message


def test_regular_http_error_redacts_tokens_and_signed_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": f"failure ebs_echoed-token at {SIGNED_URL}"})

    with make_client(handler) as client:
        with pytest.raises(CLIError) as caught:
            client.get_trigger(TRIGGER_ID)

    message = str(caught.value)
    assert "ebs_echoed-token" not in message
    assert "signed-secret" not in message
    assert "[redacted]" in message


def test_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": "https://attacker.example.test/"})

    with make_client(handler) as client:
        with pytest.raises(CLIError, match="redirects are disabled"):
            client.list_triggers()

    assert calls == 1


def test_malformed_success_response_has_actionable_error() -> None:
    with make_client(lambda request: httpx.Response(200, json={"items": "wrong"})) as client:
        with pytest.raises(CLIError, match="expected EBS API format"):
            client.list_triggers()


def test_network_error_does_not_include_request_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed with request details", request=request)

    with make_client(handler) as client:
        with pytest.raises(CLIError) as caught:
            client.get_trigger(TRIGGER_ID)

    assert "ebs_transport-test-token" not in str(caught.value)
    assert "request details" not in str(caught.value)


@pytest.mark.parametrize("key", ["", "/absolute", "../secret", "a//b", "a\\b", "bad\nkey"])
def test_unsafe_object_keys_are_rejected(key: str) -> None:
    with pytest.raises(CLIError):
        validate_object_key(key)


def test_safe_nested_object_key_is_accepted() -> None:
    assert validate_object_key("incoming/data file.fits") == "incoming/data file.fits"


def test_projects_request_uses_bearer_without_group_header_and_parses_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/projects"
        assert request.method == "GET"
        assert "X-Group" not in request.headers
        assert request.headers["Authorization"] == "Bearer ebs_transport-test-token"
        assert request.headers["Accept"] == "application/json"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"name": "dasrepo", "role": "user"},
                    {"name": "nstaff", "role": "maintainer"},
                ]
            },
        )

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        projects = client.list_projects()

    assert [(project.name, project.role) for project in projects] == [
        ("dasrepo", "user"),
        ("nstaff", "maintainer"),
    ]


def test_projects_request_can_opt_in_to_batched_trigger_counts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/projects"
        assert dict(request.url.params) == {"include_trigger_counts": "true"}
        assert "X-Group" not in request.headers
        return httpx.Response(
            200,
            json={
                "items": [
                    {"name": "dasrepo", "role": "user", "trigger_count": 0},
                    {"name": "nstaff", "role": "maintainer", "trigger_count": 12},
                ]
            },
        )

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        projects = client.list_projects(include_trigger_counts=True)

    assert len(requests) == 1
    assert [project.trigger_count for project in projects] == [0, 12]


def test_count_triggers_sends_one_bounded_page_scoped_to_the_named_project() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/triggers"
        assert request.method == "GET"
        assert request.url.params["limit"] == "1"
        assert request.url.params["offset"] == "0"
        assert request.headers["X-Group"] == "dasrepo"
        assert request.headers["Authorization"] == "Bearer ebs_transport-test-token"
        return httpx.Response(200, json={"items": [], "total": 7, "limit": 1, "offset": 0})

    # Even a client holding a different selected Project must count under the
    # explicitly named Project, never the ambient one.
    client = EBSClient(
        "https://ebs.example.test",
        "ebs_transport-test-token",
        "physics",
        transport=httpx.MockTransport(handler),
    )
    with client:
        assert client.count_triggers("dasrepo") == 7

    assert len(requests) == 1


def test_count_triggers_denied_error_mentions_the_selected_project() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Group"] == "nstaff"
        return httpx.Response(403, json={"detail": "not permitted"})

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        with pytest.raises(CLIError, match="Access denied for the selected Project"):
            client.count_triggers("nstaff")


def test_scoped_client_still_sends_group_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Group"] == "physics"
        return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})

    client = EBSClient(
        "https://ebs.example.test",
        "ebs_transport-test-token",
        "physics",
        transport=httpx.MockTransport(handler),
    )
    with client:
        page = client.list_triggers()

    assert page.items == []


def test_list_string_detail_renders_verbatim() -> None:
    """Core's pipeline render gate 422s with a list of plain sentences
    (issues/192, API-09): the CLI renders them instead of blanking the
    reason entirely."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    "outputs.subprocess: 'subprocess' is not an allowed output plugin",
                    "line 3: 'env(' is not allowed in a filter snippet",
                ]
            },
        )

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        with pytest.raises(CLIError, match="not an allowed output plugin.*not allowed in a filter snippet"):
            client.list_projects()


def test_validation_object_detail_still_renders_msg() -> None:
    """Pydantic validation 422s carry {loc, msg, type} objects; the CLI
    keeps rendering their msg (joined like the string shape)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "version"], "msg": "version required", "type": "missing"}]},
        )

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        with pytest.raises(CLIError, match="version required"):
            client.list_projects()


def test_projects_access_denied_error_is_not_project_scoped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "not permitted"})

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        with pytest.raises(CLIError, match="Access denied\\."):
            client.list_projects()


def test_projects_auth_failure_gives_existing_guidance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid token"})

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        with pytest.raises(CLIError, match="Authentication failed"):
            client.list_projects()


def test_malformed_projects_response_has_actionable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"name": "", "role": "user"}]})

    client = EBSClient(
        "https://ebs.example.test", "ebs_transport-test-token", None, transport=httpx.MockTransport(handler)
    )
    with client:
        with pytest.raises(CLIError, match="expected EBS API format"):
            client.list_projects()


def test_server_url_rejects_embedded_credentials() -> None:
    with pytest.raises(CLIError):
        normalize_server_url("https://user:password@example.test")
    assert normalize_server_url("http://localhost:8000/") == "http://localhost:8000"
