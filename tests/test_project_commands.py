"""Tests for Project discovery, the saved default, and effective-Project precedence.

These tests stand in for EBSClient so no request leaves the test process and
no live server or S3 endpoint is contacted.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from conftest import TRIGGER_ID, trigger_data
from typer.testing import CliRunner

from ebs_cli import cli
from ebs_cli.cli import Options, app
from ebs_cli.config import load_token, save_project, save_token
from ebs_cli.errors import CLIError
from ebs_cli.models import OffsetPage, Project, Trigger

CONFIG_ENV = "EBS_CONFIG"


class RecordingClient:
    """EBSClient stand-in that records constructor arguments and serves Projects."""

    last: "RecordingClient | None" = None
    projects: list[Project] | None = None
    totals: dict[str, int] = {}
    fail_count_for: str | None = None

    def __init__(self, url: str, token: str, project: str | None = None, **kwargs: Any) -> None:
        self.url = url
        self.token = token
        self.project = project
        self.calls: list[tuple[Any, ...]] = []
        RecordingClient.last = self

    def __enter__(self) -> "RecordingClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def list_projects(self, *, include_trigger_counts: bool = False) -> list[Project]:
        self.calls.append(("list_projects_with_counts" if include_trigger_counts else "list_projects",))
        if RecordingClient.projects is not None:
            return list(RecordingClient.projects)
        return [Project(name="zeta", role="user"), Project(name="alpha", role="maintainer")]

    def list_triggers(self, **kwargs: Any) -> OffsetPage[Any]:
        self.calls.append(("list_triggers", kwargs))
        return OffsetPage(items=[], total=0, limit=50, offset=0)

    def count_triggers(self, project: str) -> int:
        self.calls.append(("count_triggers", project))
        if RecordingClient.fail_count_for == project:
            raise CLIError("Access denied for the selected Project. not permitted")
        return RecordingClient.totals.get(project, 0)

    def get_trigger(self, trigger_id: Any) -> Any:
        self.calls.append(("get_trigger", trigger_id))
        return Trigger.model_validate(trigger_data())


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> type[RecordingClient]:
    monkeypatch.setattr(cli, "EBSClient", RecordingClient)
    monkeypatch.setattr(RecordingClient, "projects", None)
    monkeypatch.setattr(RecordingClient, "totals", {})
    monkeypatch.setattr(RecordingClient, "fail_count_for", None)
    RecordingClient.last = None
    return RecordingClient


def config_file() -> Path:
    return Path(os.environ[CONFIG_ENV])


def read_config_file() -> dict[str, Any]:
    return json.loads(config_file().read_text(encoding="utf-8"))


def test_projects_list_works_without_any_project_selection(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["projects", "list"], env={"COLUMNS": "120"})

    assert result.exit_code == 0, result.output
    assert RecordingClient.last is not None
    assert RecordingClient.last.project is None
    assert RecordingClient.last.token == "ebs_unit-test-token"
    assert "alpha" in result.stdout
    assert "zeta" in result.stdout
    assert "maintainer" in result.stdout
    assert "Role" in result.stdout


def test_projects_list_counts_triggers_per_project_in_request_order(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    RecordingClient.totals = {"zeta": 3, "alpha": 12}

    result = runner.invoke(app, ["projects", "list", "--json"], env={"COLUMNS": "120"})

    assert result.exit_code == 0, result.output
    assert RecordingClient.last is not None
    # Counts are requested per listed Project, one bounded page each, in the
    # same alphabetical order the table and JSON display.
    assert RecordingClient.last.calls == [
        ("list_projects_with_counts",),
        ("count_triggers", "alpha"),
        ("count_triggers", "zeta"),
    ]
    assert json.loads(result.stdout) == {
        "items": [
            {"name": "alpha", "role": "maintainer", "trigger_count": 12},
            {"name": "zeta", "role": "user", "trigger_count": 3},
        ]
    }


def test_projects_list_uses_batched_counts_without_per_project_requests(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    RecordingClient.projects = [
        Project(name=f"synthetic-{index:02d}", role="user", trigger_count=index)
        for index in reversed(range(41))
    ]

    result = runner.invoke(app, ["projects", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert RecordingClient.last is not None
    assert RecordingClient.last.calls == [("list_projects_with_counts",)]
    assert json.loads(result.stdout) == {
        "items": [
            {"name": f"synthetic-{index:02d}", "role": "user", "trigger_count": index} for index in range(41)
        ]
    }


def test_projects_list_table_shows_zero_and_large_counts_right_aligned(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    RecordingClient.totals = {"alpha": 0, "zeta": 12345}

    result = runner.invoke(app, ["projects", "list"], env={"COLUMNS": "120"})

    assert result.exit_code == 0, result.output
    assert "0" in result.stdout
    assert "12345" in result.stdout
    header_row = next(line for line in result.stdout.splitlines() if "Triggers" in line)
    columns = [column.strip() for column in header_row.split("┃") if column.strip()]
    assert columns == ["Project", "Role", "Triggers"]
    for row in (line for line in result.stdout.splitlines() if "│" in line):
        cells = [cell.strip() for cell in row.split("│") if cell.strip()]
        if not cells or cells[0] not in {"alpha", "zeta"}:
            continue
        assert cells[-1].isdigit(), row


def test_projects_list_never_counts_with_an_ambient_project(
    runner: CliRunner, recording_client: type[RecordingClient]
) -> None:
    # The autouse fixture sets EBS_PROJECT=physics and a saved default is also
    # present; neither may scope another row's trigger count.
    save_project("saved-project")
    RecordingClient.totals = {"alpha": 1, "zeta": 2}

    result = runner.invoke(app, ["projects", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert RecordingClient.last is not None
    assert RecordingClient.last.project is None
    assert [call for call in RecordingClient.last.calls if call[0] == "count_triggers"] == [
        ("count_triggers", "alpha"),
        ("count_triggers", "zeta"),
    ]


def test_projects_list_empty_project_list_makes_no_count_requests(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    RecordingClient.projects = []

    result = runner.invoke(app, ["projects", "list", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"items": []}
    assert RecordingClient.last is not None
    assert RecordingClient.last.calls == [("list_projects_with_counts",)]

    human = runner.invoke(app, ["projects", "list"])
    assert human.exit_code == 0, human.output
    assert "No Projects are associated with your account." in human.stdout


def test_projects_list_count_failure_fails_with_project_context(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    RecordingClient.fail_count_for = "zeta"

    result = runner.invoke(app, ["projects", "list"])

    assert result.exit_code == 1
    assert "Could not count triggers in Project 'zeta'" in result.stderr
    assert "Access denied for the selected Project" in result.stderr
    # The failure surfaces instead of a fabricated zero count.
    assert "Triggers" not in result.stdout


def test_projects_list_json_is_stable_and_sorted(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    first = runner.invoke(app, ["projects", "list", "--json"])
    second = runner.invoke(app, ["projects", "list", "--json"])

    assert first.exit_code == second.exit_code == 0, first.output
    assert first.stdout == second.stdout
    assert first.stdout.startswith('{"items":')
    assert json.loads(first.stdout) == {
        "items": [
            {"name": "alpha", "role": "maintainer", "trigger_count": 0},
            {"name": "zeta", "role": "user", "trigger_count": 0},
        ]
    }


def test_projects_use_validates_against_endpoint_and_saves_default(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["projects", "use", "alpha"])

    assert result.exit_code == 0, result.output
    assert "Saved default Project 'alpha'" in result.stdout
    assert str(config_file()) not in result.output
    assert RecordingClient.last is not None
    assert RecordingClient.last.project is None
    assert read_config_file() == {"project": "alpha"}


def test_projects_use_preserves_the_saved_token(
    runner: CliRunner, recording_client: type[RecordingClient]
) -> None:
    save_token("ebs_saved-test-token")

    result = runner.invoke(app, ["projects", "use", "zeta"])

    assert result.exit_code == 0, result.output
    assert "ebs_saved-test-token" not in result.output
    assert load_token(config_file()) == "ebs_saved-test-token"
    assert read_config_file() == {"token": "ebs_saved-test-token", "project": "zeta"}


def test_projects_use_unknown_project_fails_without_modifying_config(
    runner: CliRunner, recording_client: type[RecordingClient]
) -> None:
    save_token("ebs_saved-test-token")
    save_project("physics")
    before = config_file().read_text(encoding="utf-8")

    result = runner.invoke(app, ["projects", "use", "does-not-exist"])

    assert result.exit_code == 2
    assert "Unknown Project 'does-not-exist'" in result.stderr
    assert "ebs projects list" in result.stderr
    assert config_file().read_text(encoding="utf-8") == before


@pytest.mark.parametrize("name", ["", "   ", "bad\nname", "caf\xc3\xa9", "bad\tname"])
def test_projects_use_rejects_unsafe_names_without_requests(
    runner: CliRunner, recording_client: type[RecordingClient], name: str
) -> None:
    save_token("ebs_saved-test-token")
    before = config_file().read_text(encoding="utf-8")

    result = runner.invoke(app, ["projects", "use", name])

    assert result.exit_code == 2
    assert "printable ASCII" in result.stderr
    assert RecordingClient.last is None
    assert config_file().read_text(encoding="utf-8") == before


def test_projects_use_requires_authentication_guidance(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_TOKEN")
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["projects", "use", "alpha"])

    assert result.exit_code == 2
    assert "auth login" in result.stderr
    assert "EBS_TOKEN" in result.stderr
    assert RecordingClient.last is None


def test_projects_show_prefers_flag_over_environment_and_saved(
    runner: CliRunner, recording_client: type[RecordingClient]
) -> None:
    save_project("saved-project")

    result = runner.invoke(app, ["--project", "flag-project", "projects", "show", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"project": "flag-project", "source": "flag"}
    assert RecordingClient.last is None


def test_projects_show_prefers_environment_over_saved(
    runner: CliRunner, recording_client: type[RecordingClient]
) -> None:
    save_project("saved-project")

    result = runner.invoke(app, ["projects", "show", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"project": "physics", "source": "environment"}


def test_projects_show_reports_the_saved_default(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    save_project("physics")

    result = runner.invoke(app, ["projects", "show"])

    assert result.exit_code == 0, result.output
    assert "physics" in result.stdout
    assert "saved default Project" in result.stdout
    assert str(config_file()) not in result.output
    assert "Config" not in result.stdout


def test_projects_show_reports_no_default(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["projects", "show"])

    assert result.exit_code == 0, result.output
    assert "no default Project" in result.stdout
    assert "ebs projects use" in result.stdout


def test_projects_show_does_not_contact_the_api_for_saved_values(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    save_project("physics")

    human = runner.invoke(app, ["projects", "show"])
    stable = runner.invoke(app, ["projects", "show", "--json"])

    assert human.exit_code == stable.exit_code == 0, human.output
    assert RecordingClient.last is None
    assert json.loads(stable.stdout) == {"project": "physics", "source": "saved"}


def test_projects_show_reports_invalid_saved_project_value(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    config_file().write_text('{"project": "bad\\nname"}\n', encoding="utf-8")
    config_file().chmod(0o600)

    result = runner.invoke(app, ["projects", "show"])

    output = result.stdout + result.stderr
    assert result.exit_code == 1
    assert "valid default Project" in output
    assert "bad\nname" not in output
    assert str(config_file()) not in output
    assert str(Path.home()) not in output


def test_api_commands_use_the_saved_default_after_projects_use(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    save_project("alpha")

    listed = runner.invoke(app, ["triggers", "list"])
    shown = runner.invoke(app, ["triggers", "show", str(TRIGGER_ID)])

    assert listed.exit_code == shown.exit_code == 0, listed.output
    assert RecordingClient.last is not None
    assert RecordingClient.last.project == "alpha"
    assert RecordingClient.last.calls[-1] == ("get_trigger", TRIGGER_ID)


def test_api_commands_prefer_flag_over_environment_and_saved(
    runner: CliRunner, recording_client: type[RecordingClient]
) -> None:
    save_project("saved-project")

    result = runner.invoke(app, ["--project", "flag-project", "triggers", "list"])

    assert result.exit_code == 0, result.output
    assert RecordingClient.last is not None
    assert RecordingClient.last.project == "flag-project"


def test_api_commands_prefer_environment_over_saved(
    runner: CliRunner, recording_client: type[RecordingClient]
) -> None:
    save_project("saved-project")

    result = runner.invoke(app, ["triggers", "list"])

    assert result.exit_code == 0, result.output
    assert RecordingClient.last is not None
    assert RecordingClient.last.project == "physics"


def test_project_only_config_works_with_environment_authentication(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    save_project("alpha")

    result = runner.invoke(app, ["triggers", "list"])

    assert result.exit_code == 0, result.output
    assert read_config_file() == {"project": "alpha"}
    assert RecordingClient.last is not None
    assert RecordingClient.last.project == "alpha"
    assert RecordingClient.last.token == "ebs_unit-test-token"


def test_no_project_error_directs_to_projects_commands(
    runner: CliRunner, recording_client: type[RecordingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["triggers", "list"])

    assert result.exit_code == 2
    assert "ebs projects list" in result.stderr
    assert "ebs projects use PROJECT" in result.stderr


def test_resolve_project_without_any_source_reads_no_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "missing.json"))

    assert cli.resolve_project(Options("https://ebs.example.test", None, "none")) == (None, "none")
    assert cli.resolve_project(Options("https://ebs.example.test", "flag-project", "flag")) == (
        "flag-project",
        "flag",
    )
    assert cli.resolve_project(Options("https://ebs.example.test", "env-project", "environment")) == (
        "env-project",
        "environment",
    )
