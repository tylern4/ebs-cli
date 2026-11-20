import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ebs_cli.cli import app
from ebs_cli.config import (
    load_token,
    logout,
    save_project,
    save_token,
    saved_project,
    validate_project_name,
)
from ebs_cli.errors import CLIError


def test_saved_config_is_owner_only_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "private" / "config.json"

    save_token("ebs_saved-test-token", path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_token(path) == "ebs_saved-test-token"
    assert logout(path) is True
    assert not path.exists()
    assert logout(path) is False


def test_environment_token_takes_precedence_only_for_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)
    monkeypatch.setenv("EBS_TOKEN", "ebs_environment-test-token")

    assert load_token() == "ebs_environment-test-token"
    assert load_token(path) == "ebs_saved-test-token"


def test_permissive_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)
    path.chmod(0o644)

    with pytest.raises(CLIError, match="chmod 600"):
        load_token(path)


def test_symlink_config_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    save_token("ebs_saved-test-token", target)
    link = tmp_path / "config.json"
    link.symlink_to(target)

    with pytest.raises(CLIError, match="regular file"):
        load_token(link)


def test_login_prompt_does_not_echo_token_and_sets_permissions(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "login.json"
    monkeypatch.setenv("EBS_CONFIG", str(path))
    token = "ebs_interactive-test-token"

    result = runner.invoke(app, ["auth", "login"], input=f"{token}\n")

    assert result.exit_code == 0, result.output
    assert token not in result.output
    assert "owner-only" in result.stdout
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_token(path) == token


def test_logout_removes_only_the_saved_config(runner: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)

    result = runner.invoke(app, ["auth", "logout"])
    second = runner.invoke(app, ["auth", "logout"])

    assert result.exit_code == 0
    assert second.exit_code == 0
    assert "No saved" in second.stdout
    assert not path.exists()


def test_invalid_token_value_never_appears_in_error(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_value = "not-a-token-but-sensitive"

    result = runner.invoke(app, ["triggers", "list"], env={"EBS_TOKEN": bad_value})

    assert result.exit_code == 2
    assert bad_value not in result.output
    assert "ebs_" in result.stderr


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership is required")
def test_config_is_owned_by_current_user(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)

    assert path.stat().st_uid == os.getuid()


def test_saved_project_round_trips_with_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"

    save_token("ebs_saved-test-token", path)
    save_project("physics", path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert saved_project(path) == "physics"
    assert load_token(path) == "ebs_saved-test-token"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "token": "ebs_saved-test-token",
        "project": "physics",
    }


def test_save_token_preserves_the_saved_project(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_project("physics", path)

    save_token("ebs_saved-test-token", path)

    assert saved_project(path) == "physics"
    assert load_token(path) == "ebs_saved-test-token"


def test_token_only_file_migrates_unchanged_and_gains_a_project(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"token":"ebs_saved-test-token"}\n', encoding="utf-8")
    path.chmod(0o600)

    assert load_token(path) == "ebs_saved-test-token"
    assert saved_project(path) is None

    save_project("physics", path)

    assert load_token(path) == "ebs_saved-test-token"
    assert saved_project(path) == "physics"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "token": "ebs_saved-test-token",
        "project": "physics",
    }


def test_project_only_config_with_environment_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    save_project("physics", path)
    monkeypatch.setenv("EBS_TOKEN", "ebs_environment-test-token")

    assert saved_project(path) == "physics"
    assert load_token() == "ebs_environment-test-token"
    with pytest.raises(CLIError, match="valid API token"):
        load_token(path)


def test_logout_removes_only_the_token_and_keeps_the_project(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)
    save_project("physics", path)

    assert logout(path) is True
    assert path.exists()
    assert saved_project(path) == "physics"
    assert "token" not in json.loads(path.read_text(encoding="utf-8"))
    assert logout(path) is False
    assert path.exists()
    assert saved_project(path) == "physics"


def test_logout_removes_the_file_when_no_settings_remain(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)

    assert logout(path) is True
    assert not path.exists()


def test_invalid_saved_project_value_is_rejected_without_echo(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"token":"ebs_saved-test-token","project":"bad\\nname"}\n', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CLIError, match="valid default Project"):
        saved_project(path)


def test_save_project_rejects_permissive_existing_config_and_leaves_it_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)
    path.chmod(0o644)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(CLIError, match="chmod 600"):
        save_project("physics", path)

    assert path.read_text(encoding="utf-8") == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_save_project_rejects_symlink_and_leaves_target_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    save_token("ebs_saved-test-token", target)
    link = tmp_path / "config.json"
    link.symlink_to(target)

    with pytest.raises(CLIError, match="regular file"):
        save_project("physics", link)

    assert json.loads(target.read_text(encoding="utf-8")) == {"token": "ebs_saved-test-token"}


def test_save_project_rejects_invalid_names_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)
    before = path.read_text(encoding="utf-8")

    for name in ("", "   ", "bad\nname", "caf\xc3\xa9"):
        with pytest.raises(CLIError):
            save_project(name, path)

    assert path.read_text(encoding="utf-8") == before


def test_validate_project_name_strips_and_enforces_printable_ascii() -> None:
    assert validate_project_name("  physics  ") == "physics"

    for name in ("", "   ", "bad\nname", "caf\xc3\xa9", "bad\tname"):
        with pytest.raises(CLIError, match="printable ASCII"):
            validate_project_name(name)


def test_login_preserves_saved_project_and_never_echoes_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setenv("EBS_CONFIG", str(path))
    save_project("physics", path)

    result = runner.invoke(app, ["auth", "login"], input="ebs_interactive-test-token\n")

    assert result.exit_code == 0, result.output
    assert "ebs_interactive-test-token" not in result.output
    assert "The saved default Project remains" in result.stdout
    assert load_token(path) == "ebs_interactive-test-token"
    assert saved_project(path) == "physics"


def test_logout_command_preserves_saved_project(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    monkeypatch.setenv("EBS_CONFIG", str(path))
    save_token("ebs_saved-test-token", path)
    save_project("physics", path)

    result = runner.invoke(app, ["auth", "logout"])

    assert result.exit_code == 0
    assert "The saved default Project remains" in result.stdout
    assert saved_project(path) == "physics"
    assert "token" not in json.loads(path.read_text(encoding="utf-8"))
