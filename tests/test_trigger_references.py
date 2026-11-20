"""Tests for typed trigger references: UUIDs skip lookup, names resolve through
one bounded, Project-scoped, exact-match listing used by every command family.

These tests stand in for EBSClient so no request leaves the test process and
no live server or S3 endpoint is contacted.
"""

import json

import httpx
import pytest
from conftest import EVENT_ID, SIGNED_URL, TRIGGER_ID, FakeAPI, trigger_data
from typer.testing import CliRunner

from ebs_cli import cli
from ebs_cli.cli import app
from ebs_cli.client import NAME_LOOKUP_LIMIT, EBSClient, resolve_trigger_reference, validate_trigger_name
from ebs_cli.config import save_project
from ebs_cli.errors import CLIError
from ebs_cli.models import OffsetPage, Trigger

OTHER_ID = "99999999-8888-7777-6666-555555555555"
TOKEN = "ebs_unit-test-token"
SECRETS = ("temporary-access", "temporary-secret", "temporary-session", SIGNED_URL)
FAMILIES = (
    "show",
    "enable",
    "disable",
    "events",
    "credentials",
    "upload-url",
    "upload",
    "ls",
    "download",
    "rm",
)

S3_OBJECT_PATCHES = {
    "upload": ("ebs_cli.cli.upload_file", lambda *args, **kwargs: None),
    "ls": ("ebs_cli.cli.list_objects", lambda *args, **kwargs: {"items": [], "next_cursor": None}),
    "download": ("ebs_cli.cli.download_object", lambda *args, **kwargs: None),
    "rm": ("ebs_cli.cli.delete_object", lambda *args, **kwargs: None),
}


def family_argv(family: str, reference: str, tmp_path) -> list[str]:
    local = tmp_path / "local-data.fits"
    local.write_bytes(b"fits")
    destination = tmp_path / "a.fits"
    return {
        "show": ["triggers", "show", reference],
        "enable": ["triggers", "enable", reference],
        "disable": ["triggers", "disable", reference],
        "events": ["events", "list", reference],
        "credentials": ["s3", "credentials", reference],
        "upload-url": ["s3", "upload-url", reference, "data.fits"],
        "upload": ["s3", "upload", reference, str(local)],
        "ls": ["s3", "ls", reference],
        "download": ["s3", "download", reference, "run/a.fits", str(destination)],
        "rm": ["s3", "rm", reference, "run/a.fits", "--yes"],
    }[family]


def downstream_call(family: str) -> tuple:
    return {
        "show": ("get_trigger", TRIGGER_ID),
        "enable": ("set_trigger_enabled", TRIGGER_ID, True),
        "disable": ("set_trigger_enabled", TRIGGER_ID, False),
        "events": ("list_events", TRIGGER_ID, {"limit": 50, "cursor": None, "status": None, "source": None}),
        "credentials": ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": "upload"}),
        "upload-url": ("s3_upload_url", TRIGGER_ID, "data.fits", {"duration_seconds": 900}),
        "upload": ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": "upload"}),
        "ls": ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": "list"}),
        "download": ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": "download"}),
        "rm": ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": "delete"}),
    }[family]


def lookup_call(reference: str) -> tuple:
    return ("list_triggers", {"name": reference, "limit": NAME_LOOKUP_LIMIT, "offset": 0})


def run_family(
    runner: CliRunner,
    fake_api: FakeAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    family: str,
    reference: str,
):
    if family in S3_OBJECT_PATCHES:
        target, value = S3_OBJECT_PATCHES[family]
        monkeypatch.setattr(target, value)
    return runner.invoke(app, family_argv(family, reference, tmp_path), env={"COLUMNS": "200"})


def trigger_page(*entries: tuple[str, str], total: int | None = None) -> OffsetPage[Trigger]:
    items = [Trigger.model_validate(trigger_data(id=identifier, name=name)) for identifier, name in entries]
    return OffsetPage(
        items=items, total=total if total is not None else len(items), limit=NAME_LOOKUP_LIMIT, offset=0
    )


# --- UUID references keep the direct flow with no extra list request ---------


@pytest.mark.parametrize("family", FAMILIES)
def test_uuid_reference_skips_name_lookup_in_every_family(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path, family: str
) -> None:
    result = run_family(runner, fake_api, monkeypatch, tmp_path, family, str(TRIGGER_ID))

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [downstream_call(family)]


@pytest.mark.parametrize("family", FAMILIES)
def test_exact_name_resolves_through_one_bounded_listing_in_every_family(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path, family: str
) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"))

    result = run_family(runner, fake_api, monkeypatch, tmp_path, family, "data-ready")

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [lookup_call("data-ready"), downstream_call(family)]


# --- Failed resolution: absent, ambiguous, case-sensitive, truncated ---------


def test_absent_name_fails_with_guidance_and_no_downstream_request(
    runner: CliRunner, fake_api: FakeAPI
) -> None:
    fake_api.trigger_page = trigger_page()

    result = runner.invoke(app, ["triggers", "show", "data-ready"])

    assert result.exit_code == 2
    assert "No trigger named 'data-ready' exists in the selected Project." in result.stderr
    assert "ebs triggers list --name data-ready" in result.stderr
    assert fake_api.calls == [lookup_call("data-ready")]


def test_partial_and_case_insensitive_matches_never_resolve(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "Data-Ready"))

    for reference in ("data-ready", "data", "DATA-READY"):
        result = runner.invoke(app, ["triggers", "show", reference])

        assert result.exit_code == 2, result.output
        assert f"No trigger named '{reference}' exists" in result.stderr
    assert fake_api.calls == [lookup_call(reference) for reference in ("data-ready", "data", "DATA-READY")]


def test_ambiguous_exact_matches_fail_closed_with_candidate_ids(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"), (OTHER_ID, "data-ready"))

    result = runner.invoke(app, ["triggers", "show", "data-ready"])

    assert result.exit_code == 2
    assert "ambiguous" in result.stderr
    assert str(TRIGGER_ID) in result.stderr
    assert OTHER_ID in result.stderr
    assert "Use a trigger ID instead." in result.stderr
    assert fake_api.calls == [lookup_call("data-ready")]


def test_more_candidates_than_one_bounded_page_fail_closed(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page(total=NAME_LOOKUP_LIMIT + 50)

    result = runner.invoke(app, ["triggers", "show", "data-ready"])

    assert result.exit_code == 2
    assert "bounded" in result.stderr
    assert "Use a trigger ID instead." in result.stderr
    assert fake_api.calls == [lookup_call("data-ready")]


def test_single_on_page_match_is_rejected_when_candidates_exceed_the_page(
    runner: CliRunner, fake_api: FakeAPI
) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"), total=NAME_LOOKUP_LIMIT + 1)

    result = runner.invoke(app, ["triggers", "show", "data-ready"])

    assert result.exit_code == 2
    assert "bounded" in result.stderr
    assert "Use a trigger ID instead." in result.stderr
    assert fake_api.calls == [lookup_call("data-ready")]


def test_resolution_failures_leak_no_token_or_temporary_secrets(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"), (OTHER_ID, "data-ready"))

    ambiguous = runner.invoke(app, ["triggers", "show", "data-ready"])
    fake_api.trigger_page = trigger_page()
    absent = runner.invoke(app, ["triggers", "show", "data-ready"])

    for result in (ambiguous, absent):
        assert result.exit_code == 2
        assert TOKEN not in result.output
        for secret in SECRETS:
            assert secret not in result.output


def test_http_failure_during_resolution_is_a_safe_error(runner: CliRunner, fake_api: FakeAPI) -> None:
    def fail(**kwargs):
        raise CLIError("EBS server failed with HTTP 500. Retry later.")

    fake_api.list_triggers = fail

    result = runner.invoke(app, ["triggers", "show", "data-ready"])

    assert result.exit_code == 1
    assert "HTTP 500" in result.stderr
    assert TOKEN not in result.output


@pytest.mark.parametrize("reference", ["", "bad\nname", "x" * 256])
def test_invalid_references_fail_before_any_request(
    runner: CliRunner, fake_api: FakeAPI, reference: str
) -> None:
    result = runner.invoke(app, ["triggers", "show", reference])

    assert result.exit_code == 2
    assert "Trigger name" in result.stderr
    assert fake_api.calls == []


# --- triggers list --name filter ---------------------------------------------


def test_list_name_filter_reaches_the_bounded_page_parameters(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["triggers", "list", "--limit", "7", "--offset", "3", "--name", "data"])

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [("list_triggers", {"limit": 7, "offset": 3, "enabled": None, "name": "data"})]


def test_list_name_filter_combines_with_state_filter(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["triggers", "list", "--enabled", "--name", "data"])

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [("list_triggers", {"limit": 50, "offset": 0, "enabled": True, "name": "data"})]


@pytest.mark.parametrize("name", ["", "x" * 256, "bad\nname"])
def test_list_name_filter_is_validated_before_any_request(
    runner: CliRunner, fake_api: FakeAPI, name: str
) -> None:
    result = runner.invoke(app, ["triggers", "list", "--name", name])

    assert result.exit_code == 2
    assert "Trigger name" in result.stderr
    assert fake_api.calls == []


# --- Project precedence and scoping ------------------------------------------


class ResolverClient:
    """EBSClient stand-in recording the Project used for name resolution."""

    last: "ResolverClient | None" = None

    def __init__(self, url: str, token: str, project: str | None = None, **kwargs) -> None:
        self.project = project
        self.calls: list[tuple] = []
        ResolverClient.last = self

    def __enter__(self) -> "ResolverClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    def list_triggers(self, **kwargs) -> OffsetPage[Trigger]:
        self.calls.append(("list_triggers", kwargs))
        return trigger_page((str(TRIGGER_ID), "data-ready"))

    def get_trigger(self, trigger_id) -> Trigger:
        self.calls.append(("get_trigger", trigger_id))
        return Trigger.model_validate(trigger_data())


def test_name_resolution_uses_the_saved_default_project(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    monkeypatch.setattr(cli, "EBSClient", ResolverClient)
    save_project("alpha")

    result = runner.invoke(app, ["triggers", "show", "data-ready"])

    assert result.exit_code == 0, result.output
    assert ResolverClient.last is not None
    assert ResolverClient.last.project == "alpha"
    assert ResolverClient.last.calls == [lookup_call("data-ready"), ("get_trigger", TRIGGER_ID)]


def test_resolution_never_requests_another_project() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Group"] == "physics"
        assert request.url.params["name"] == "data-ready"
        assert request.url.params["limit"] == str(NAME_LOOKUP_LIMIT)
        assert request.url.params["offset"] == "0"
        return httpx.Response(
            200, json={"items": [trigger_data()], "total": 1, "limit": NAME_LOOKUP_LIMIT, "offset": 0}
        )

    with EBSClient(
        "https://ebs.example.test",
        "ebs_transport-test-token",
        "physics",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert resolve_trigger_reference(client, "data-ready") == TRIGGER_ID

    assert len(requests) == 1


def test_resolution_http_denial_is_a_safe_project_scoped_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "not a member of this Project"})

    with EBSClient(
        "https://ebs.example.test",
        "ebs_transport-test-token",
        "physics",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(CLIError, match="Access denied for the selected Project"):
            resolve_trigger_reference(client, "data-ready")


def test_list_triggers_sends_the_name_parameter_without_enables() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params == httpx.QueryParams({"limit": "50", "offset": "0", "name": "data"})
        return httpx.Response(200, json={"items": [], "total": 0, "limit": 50, "offset": 0})

    with EBSClient(
        "https://ebs.example.test",
        "ebs_transport-test-token",
        "physics",
        transport=httpx.MockTransport(handler),
    ) as client:
        page = client.list_triggers(name="data")

    assert page.items == []
    assert page.total == 0


# --- Output compatibility ----------------------------------------------------


def test_name_resolution_json_matches_uuid_schema(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"))

    shown = runner.invoke(app, ["triggers", "show", "data-ready", "--json"])
    events = runner.invoke(app, ["events", "list", "data-ready", "--json"])

    assert shown.exit_code == events.exit_code == 0, shown.output + events.output
    body = json.loads(shown.stdout)
    assert body["id"] == str(TRIGGER_ID)
    assert body["name"] == "data-ready"
    events_body = json.loads(events.stdout)
    assert events_body["items"][0]["id"] == str(EVENT_ID)


def test_events_title_renders_reference_markup_literally(runner: CliRunner, fake_api: FakeAPI) -> None:
    reference = "[red]data-ready[/red]"
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), reference))

    result = runner.invoke(app, ["events", "list", reference])

    assert result.exit_code == 0, result.output
    assert reference in result.stdout


def test_rm_confirmation_shows_the_original_reference(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ebs_cli.cli._stdin_is_interactive", lambda: True)
    monkeypatch.setattr("ebs_cli.cli.delete_object", lambda *args, **kwargs: None)
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"))

    result = runner.invoke(app, ["s3", "rm", "data-ready", "run/a.fits"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Permanently delete" in result.stdout
    assert "data-ready" in result.stdout
    assert fake_api.calls == [lookup_call("data-ready"), downstream_call("rm")]


# --- Help surfaces the typed reference ---------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["triggers", "show", "--help"],
        ["triggers", "enable", "--help"],
        ["triggers", "disable", "--help"],
        ["events", "list", "--help"],
        ["s3", "credentials", "--help"],
        ["s3", "upload-url", "--help"],
        ["s3", "upload", "--help"],
        ["s3", "ls", "--help"],
        ["s3", "download", "--help"],
        ["s3", "rm", "--help"],
    ],
)
def test_trigger_argument_help_names_the_reference(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 0
    assert "TRIGGER_REF" in result.stdout
    assert "exact trigger name" in result.stdout


def test_list_help_shows_the_name_filter(runner: CliRunner) -> None:
    result = runner.invoke(app, ["triggers", "list", "--help"], env={"COLUMNS": "200"})

    assert result.exit_code == 0
    assert "--name" in result.stdout


# --- Validation helper -------------------------------------------------------


def test_validate_trigger_name_bounds() -> None:
    assert validate_trigger_name("data-ready") == "data-ready"
    assert validate_trigger_name("x" * 255) == "x" * 255
    with pytest.raises(CLIError):
        validate_trigger_name("")
    with pytest.raises(CLIError):
        validate_trigger_name("x" * 256)
    with pytest.raises(CLIError):
        validate_trigger_name("bad\nname")
