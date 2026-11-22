import json

from conftest import TRIGGER_ID, FakeAPI
from typer.testing import CliRunner

from ebs_cli.cli import app


def test_trigger_list_passes_bounds_and_filters(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(
        app, ["triggers", "list", "--limit", "7", "--offset", "3", "--disabled"], env={"COLUMNS": "180"}
    )

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [("list_triggers", {"limit": 7, "offset": 3, "enabled": False})]
    assert "data-ready" in result.stdout
    assert "Triggers" in result.stdout


def test_trigger_list_json_is_stable(runner: CliRunner, fake_api: FakeAPI) -> None:
    first = runner.invoke(app, ["triggers", "list", "--json"])
    second = runner.invoke(app, ["triggers", "list", "--json"])

    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout
    body = json.loads(first.stdout)
    assert body["items"][0]["id"] == str(TRIGGER_ID)
    assert body["total"] == 1
    assert first.stdout.startswith('{"items":')


def test_trigger_show_and_lifecycle_commands(runner: CliRunner, fake_api: FakeAPI) -> None:
    shown = runner.invoke(app, ["triggers", "show", str(TRIGGER_ID)])
    enabled = runner.invoke(app, ["triggers", "enable", str(TRIGGER_ID), "--json"])
    disabled = runner.invoke(app, ["triggers", "disable", str(TRIGGER_ID)])

    assert shown.exit_code == enabled.exit_code == disabled.exit_code == 0
    assert "Project" in shown.stdout
    assert fake_api.calls == [
        ("get_trigger", TRIGGER_ID),
        ("set_trigger_enabled", TRIGGER_ID, True),
        ("set_trigger_enabled", TRIGGER_ID, False),
    ]
    assert json.loads(enabled.stdout)["enabled"] is True


def test_project_selection_is_required_before_client_creation(runner: CliRunner, monkeypatch) -> None:
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["triggers", "list"])

    assert result.exit_code == 2
    assert "Project" in result.stderr


def test_command_http_failure_is_nonzero_and_does_not_leak_token(
    runner: CliRunner, fake_api: FakeAPI
) -> None:
    from ebs_cli.errors import CLIError

    def fail(**kwargs):
        raise CLIError("EBS server returned HTTP 500. Retry later.")

    fake_api.list_triggers = fail

    result = runner.invoke(app, ["triggers", "list"])

    assert result.exit_code == 1
    assert "HTTP 500" in result.stderr
    assert "ebs_unit-test-token" not in result.output


def test_version_and_bare_help_succeed(runner: CliRunner) -> None:
    version = runner.invoke(app, ["--version"])
    bare = runner.invoke(app, [])

    assert version.exit_code == 0
    assert version.stdout.strip() == "0.1.1"
    assert bare.exit_code == 0
    assert "Usage:" in bare.stdout


def test_token_option_is_only_a_hidden_prompt_switch(runner: CliRunner) -> None:
    login_help = runner.invoke(app, ["auth", "login", "--help"])
    root_help = runner.invoke(app, ["--help"])

    assert login_help.exit_code == root_help.exit_code == 0
    assert "--token" in login_help.stdout
    assert "hidden" in login_help.stdout
    assert "--no-browser" in login_help.stdout
    assert "--token" not in root_help.stdout

    result = runner.invoke(app, ["auth", "login", "--token=ebs_not-an-allowed-value"])
    assert result.exit_code != 0
    assert "ebs_not-an-allowed-value" not in result.output
