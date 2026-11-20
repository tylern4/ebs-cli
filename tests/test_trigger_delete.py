"""Tests for ``ebs triggers delete``: exact Project-scoped reference resolution,
default-No confirmation with the event/job history warning, required
noninteractive ``--yes``, and a typed DELETE that accepts an empty 204.

These tests are fully mocked: no live endpoint, credentials, or S3 calls.
"""

import json
from uuid import UUID

import httpx
import pytest
from conftest import TRIGGER_ID, FakeAPI, trigger_data
from rich.text import Text
from typer.testing import CliRunner

from ebs_cli import cli
from ebs_cli.cli import app
from ebs_cli.client import NAME_LOOKUP_LIMIT, EBSClient, resolve_trigger_reference
from ebs_cli.errors import CLIError
from ebs_cli.models import OffsetPage, Trigger

TOKEN = "ebs_unit-test-token"


def trigger_page(*entries: tuple[str, str], total: int | None = None) -> OffsetPage[Trigger]:
    items = [Trigger.model_validate(trigger_data(id=identifier, name=name)) for identifier, name in entries]
    return OffsetPage(
        items=items, total=total if total is not None else len(items), limit=NAME_LOOKUP_LIMIT, offset=0
    )


def lookup_call(reference: str) -> tuple:
    return ("list_triggers", {"name": reference, "limit": NAME_LOOKUP_LIMIT, "offset": 0})


# --- Client: DELETE method, path, and empty 204 success -----------------------


def test_delete_sends_typed_delete_to_trigger_path_and_accepts_empty_204() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "DELETE"
        assert request.url.path == f"/api/v1/triggers/{TRIGGER_ID}"
        assert request.headers["X-Group"] == "physics"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(204)

    with EBSClient(
        "https://ebs.example.test", TOKEN, "physics", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.delete_trigger(TRIGGER_ID) is None

    assert len(requests) == 1


def test_delete_204_never_attempts_json_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, headers={"Content-Type": "application/json"})

    with EBSClient(
        "https://ebs.example.test", TOKEN, "physics", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.delete_trigger(TRIGGER_ID) is None


def test_delete_authorization_denial_is_project_scoped_and_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "maintainer role required"})

    with EBSClient(
        "https://ebs.example.test", TOKEN, "physics", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(CLIError, match="Access denied for the selected Project"):
            client.delete_trigger(TRIGGER_ID)


def test_delete_missing_trigger_is_a_safe_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "no such trigger"})

    with EBSClient(
        "https://ebs.example.test", TOKEN, "physics", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(CLIError, match="not found"):
            client.delete_trigger(TRIGGER_ID)


def test_delete_stays_within_the_selected_project() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Group"] == "physics"
        return httpx.Response(204)

    with EBSClient(
        "https://ebs.example.test", TOKEN, "physics", transport=httpx.MockTransport(handler)
    ) as client:
        resolved = resolve_trigger_reference(client, str(TRIGGER_ID))

    assert resolved == TRIGGER_ID


# --- CLI: UUID and exact-name resolution --------------------------------------


def test_delete_uuid_with_yes_shows_identity_then_deletes(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID), "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [("get_trigger", TRIGGER_ID), ("delete_trigger", TRIGGER_ID)]
    assert "Trigger deleted" in result.stdout
    assert "data-ready" in result.stdout


def test_delete_exact_name_resolves_through_the_bounded_listing(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"))

    result = runner.invoke(app, ["triggers", "delete", "data-ready", "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [
        lookup_call("data-ready"),
        ("get_trigger", TRIGGER_ID),
        ("delete_trigger", TRIGGER_ID),
    ]


def test_delete_yes_does_not_bypass_reference_resolution(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page()

    result = runner.invoke(app, ["triggers", "delete", "data-ready", "--yes"])

    assert result.exit_code == 2
    assert "No trigger named 'data-ready' exists in the selected Project." in result.stderr
    assert fake_api.calls == [lookup_call("data-ready")]


def test_delete_ambiguous_name_fails_closed_without_deleting(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page(
        (str(TRIGGER_ID), "data-ready"), ("99999999-8888-7777-6666-555555555555", "data-ready")
    )

    result = runner.invoke(app, ["triggers", "delete", "data-ready", "--yes"])

    assert result.exit_code == 2
    assert "ambiguous" in result.stderr
    assert fake_api.calls == [lookup_call("data-ready")]


# --- CLI: confirmation gates ---------------------------------------------------


def test_delete_noninteractive_session_requires_yes_before_any_request(
    runner: CliRunner, fake_api: FakeAPI
) -> None:
    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID)])

    assert result.exit_code == 2
    assert "--yes" in result.stderr
    assert fake_api.calls == []


def test_delete_interactive_acceptance_proceeds_to_delete(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID)], input="y\n")

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [("get_trigger", TRIGGER_ID), ("delete_trigger", TRIGGER_ID)]
    assert "Permanently delete this trigger and its event and job history?" in result.stdout


def test_delete_interactive_prompt_shows_identity_and_history_warning(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID)], input="n\n")

    assert result.exit_code != 0
    assert "data-ready" in result.stdout
    assert str(TRIGGER_ID) in result.stdout
    assert "physics" in result.stdout
    assert "event and job history" in result.stdout


def test_delete_interactive_decline_sends_no_delete(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID)], input="n\n")

    assert result.exit_code != 0
    assert "Deletion cancelled. Nothing was deleted." in result.stdout
    assert fake_api.calls == [("get_trigger", TRIGGER_ID)]


def test_delete_eof_sends_no_delete(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID)], input="")

    assert result.exit_code != 0
    assert not any(call[0] == "delete_trigger" for call in fake_api.calls)
    assert fake_api.calls == [("get_trigger", TRIGGER_ID)]


def test_delete_default_answer_is_no(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID)], input="\n")

    assert result.exit_code != 0
    assert "Deletion cancelled. Nothing was deleted." in result.stdout
    assert fake_api.calls == [("get_trigger", TRIGGER_ID)]


def test_delete_json_interactive_acceptance_keeps_stdout_machine_readable(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive --json keeps stdout machine-readable: the confirmation prompt
    is routed to stderr and only the JSON document belongs on stdout.

    The test runner additionally echoes the typed keystroke to its captured
    stdout (the same characters a terminal would echo locally), so the
    assertion strips that echo before parsing.
    """
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID), "--json"], input="y\n")

    assert result.exit_code == 0, result.output
    document = result.stdout.lstrip("y \n")
    assert document.startswith("{")
    body = json.loads(document)
    assert body["id"] == str(TRIGGER_ID)
    assert body["name"] == "data-ready"
    assert "Permanently delete" not in result.stdout
    assert "Permanently delete" in result.stderr
    assert fake_api.calls == [("get_trigger", TRIGGER_ID), ("delete_trigger", TRIGGER_ID)]


def test_delete_json_interactive_decline_sends_no_delete_and_keeps_stdout_clean(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive --json decline writes no JSON, prompt, or cancellation text
    to stdout; the confirmation question and cancellation notice go to stderr.
    """
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID), "--json"], input="n\n")

    assert result.exit_code != 0
    # Only the runner's keystroke echo may reach stdout.
    assert result.stdout.strip("n \n") == ""
    assert "{" not in result.stdout
    assert "Deletion cancelled" not in result.stdout
    assert fake_api.calls == [("get_trigger", TRIGGER_ID)]
    assert "Permanently delete this trigger and its event and job history?" in result.stderr
    assert "Deletion cancelled. Nothing was deleted." in result.stderr


# --- CLI: output and failure conventions --------------------------------------


def test_delete_json_success_matches_the_trigger_schema(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID), "--yes", "--json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["id"] == str(TRIGGER_ID)
    assert body["name"] == "data-ready"
    assert "Trigger deleted" not in result.stdout


def test_delete_json_success_resolves_the_exact_name(runner: CliRunner, fake_api: FakeAPI) -> None:
    fake_api.trigger_page = trigger_page((str(TRIGGER_ID), "data-ready"))

    result = runner.invoke(app, ["triggers", "delete", "data-ready", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == str(TRIGGER_ID)


def test_delete_authorization_failure_emits_no_success_output(runner: CliRunner, fake_api: FakeAPI) -> None:
    def deny(trigger_id: UUID) -> None:
        raise CLIError("Access denied for the selected Project. Maintainer role is required.")

    fake_api.delete_trigger = deny

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID), "--yes", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Access denied" in result.stderr


def test_delete_not_found_failure_emits_no_delete_request(runner: CliRunner, fake_api: FakeAPI) -> None:
    def missing(trigger_id: UUID) -> Trigger:
        raise CLIError("Requested EBS resource was not found.")

    fake_api.get_trigger = missing

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID), "--yes"])

    assert result.exit_code == 1
    assert "not found" in result.stderr
    assert fake_api.calls == []


# --- Related commands keep their prior behavior --------------------------------


def test_enable_and_disable_keep_prior_behavior_without_deleting(
    runner: CliRunner, fake_api: FakeAPI
) -> None:
    enabled = runner.invoke(app, ["triggers", "enable", str(TRIGGER_ID), "--json"])
    disabled = runner.invoke(app, ["triggers", "disable", str(TRIGGER_ID)])

    assert enabled.exit_code == disabled.exit_code == 0
    assert fake_api.calls == [
        ("set_trigger_enabled", TRIGGER_ID, True),
        ("set_trigger_enabled", TRIGGER_ID, False),
    ]


def test_delete_help_names_the_reference_and_yes_option(runner: CliRunner) -> None:
    result = runner.invoke(app, ["triggers", "delete", "--help"], env={"COLUMNS": "200"})

    assert result.exit_code == 0
    assert "TRIGGER_REF" in result.stdout
    assert "exact trigger name" in result.stdout
    plain_help = Text.from_ansi(result.stdout).plain
    assert "--yes" in plain_help
    assert "Skip the interactive confirmation." in plain_help


def test_delete_confirmation_failure_leaks_no_token(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["triggers", "delete", str(TRIGGER_ID)], input="n\n")

    assert TOKEN not in result.output
