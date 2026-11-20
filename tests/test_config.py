import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ebs_cli.cli import app
from ebs_cli.config import load_token, logout, save_token
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
