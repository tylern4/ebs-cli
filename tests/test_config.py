import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ebs_cli.config as config_module
from ebs_cli.cli import app
from ebs_cli.config import (
    load_globus_credentials,
    load_token,
    logout,
    optional_globus_credentials,
    read_auth_snapshot,
    read_config,
    save_auth_credentials,
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


def _assert_private_cli_error(result, path: Path, expected: str, *other_paths: Path) -> None:
    output = result.stdout + result.stderr
    assert result.exit_code != 0
    assert expected in output
    assert str(path) not in output
    assert str(Path.home()) not in output
    for private_path in other_paths:
        assert str(private_path) not in output


def test_permissive_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_token("ebs_saved-test-token", path)
    path.chmod(0o644)

    with pytest.raises(CLIError, match="chmod 600") as caught:
        load_token(path)

    assert caught.value.path == path
    assert str(path) not in str(caught.value)


def test_cli_read_error_never_prints_config_or_home_paths(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_module.config_path()
    path.write_text("{malformed-json\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["projects", "show"])

    _assert_private_cli_error(result, path, "Could not read config file")


def test_cli_permissions_error_never_prints_config_or_home_paths(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_module.config_path()
    save_token("ebs_saved-test-token", path)
    path.chmod(0o644)
    monkeypatch.delenv("EBS_PROJECT")

    result = runner.invoke(app, ["projects", "show"])

    _assert_private_cli_error(result, path, "chmod 600 CONFIG_FILE")


def test_cli_lock_error_never_prints_config_lock_or_home_paths(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = config_module.config_path()
    lock_path = path.with_name(f".{path.name}.lock")
    monkeypatch.setattr(
        config_module.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied"))
    )

    result = runner.invoke(app, ["auth", "login", "--token"], input="ebs_manual-test-token\n")

    _assert_private_cli_error(result, path, "Could not lock config file", lock_path)


def test_cli_save_error_never_prints_config_or_home_paths(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = config_module.config_path()
    monkeypatch.setattr(config_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("full")))

    result = runner.invoke(app, ["auth", "login", "--token"], input="ebs_manual-test-token\n")

    _assert_private_cli_error(result, path, "Could not save config file")


def test_cli_logout_error_never_prints_config_or_home_paths(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = config_module.config_path()
    save_token("ebs_saved-test-token", path)
    monkeypatch.setattr(
        config_module.Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied"))
    )

    result = runner.invoke(app, ["auth", "logout"])

    _assert_private_cli_error(result, path, "Could not remove config file after logout")


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

    result = runner.invoke(app, ["auth", "login", "--token"], input=f"{token}\n")

    assert result.exit_code == 0, result.output
    assert token not in result.output
    assert str(path) not in result.output
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


def test_manual_token_replaces_globus_login_and_preserves_other_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_old-token", "old-refresh", "old-client", path)
    save_project("physics", path)
    body = read_config(path)
    body["custom"] = {"keep": True}
    path.write_text(json.dumps(body) + "\n", encoding="utf-8")
    path.chmod(0o600)

    save_token("ebs_manual-token", path)

    assert read_config(path) == {
        "token": "ebs_manual-token",
        "project": "physics",
        "custom": {"keep": True},
    }
    assert optional_globus_credentials(path) is None


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

    result = runner.invoke(app, ["auth", "login", "--token"], input="ebs_interactive-test-token\n")

    assert result.exit_code == 0, result.output
    assert "ebs_interactive-test-token" not in result.output
    assert "The saved default Project remains" in result.stdout
    assert load_token(path) == "ebs_interactive-test-token"
    assert saved_project(path) == "physics"


def test_browser_credentials_use_one_private_update_and_preserve_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        '{"project":"physics","token":"ebs_old-token","custom":{"keep":true}}\n',
        encoding="utf-8",
    )
    path.chmod(0o600)

    writes = []
    original_replace = os.replace

    def observe_replace(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        writes.append(target)
        original_replace(source, target)

    import ebs_cli.config as config_module

    original = config_module.os.replace
    config_module.os.replace = observe_replace
    try:
        save_auth_credentials("ebs_new-token", "refresh-test-credential", "native-client", path)
    finally:
        config_module.os.replace = original

    assert len(writes) == 1
    assert load_token(path) == "ebs_new-token"
    assert load_globus_credentials(path) == ("refresh-test-credential", "native-client")
    assert saved_project(path) == "physics"
    assert json.loads(path.read_text(encoding="utf-8"))["custom"] == {"keep": True}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_auth_write_failure_leaves_previous_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_old-token", "old-refresh", "old-client", path)
    save_project("physics", path)
    before = path.read_bytes()

    monkeypatch.setattr("ebs_cli.config.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("full")))

    with pytest.raises(CLIError, match="Could not save"):
        save_auth_credentials("ebs_new-token", "new-refresh", "new-client", path)

    assert path.read_bytes() == before
    lock_path = path.with_name(f".{path.name}.lock")
    assert lock_path.exists()
    assert list(tmp_path.glob(".config.*")) == [lock_path]


def test_atomic_commit_has_no_post_replace_chmod_and_ignores_lock_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_old-token", "old-refresh", "old-client", path)
    save_project("physics", path)

    def forbidden_post_commit_chmod(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("os.chmod must not run after the atomic replace commit point")

    real_flock = config_module.fcntl.flock

    def release_failure(descriptor: int, operation: int) -> None:
        if operation == config_module.fcntl.LOCK_UN:
            raise OSError("simulated unlock failure")
        real_flock(descriptor, operation)

    monkeypatch.setattr(config_module.os, "chmod", forbidden_post_commit_chmod)
    monkeypatch.setattr(config_module.fcntl, "flock", release_failure)

    result = save_auth_credentials("ebs_new-token", "new-refresh", "new-client", path)

    assert result == path
    assert load_token(path) == "ebs_new-token"
    assert load_globus_credentials(path) == ("new-refresh", "new-client")
    assert saved_project(path) == "physics"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("unsafe_lock", ["symlink", "permissive", "directory"])
def test_unsafe_sibling_lock_fails_before_reading_or_writing_credentials(
    tmp_path: Path,
    unsafe_lock: str,
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_old-token", "old-refresh-secret", "old-client", path)
    before = path.read_bytes()
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.unlink()
    if unsafe_lock == "symlink":
        lock_path.symlink_to(path)
    elif unsafe_lock == "permissive":
        lock_path.write_text("unevaluated-lock-contents", encoding="utf-8")
        lock_path.chmod(0o644)
    else:
        lock_path.mkdir()

    with pytest.raises(CLIError) as caught:
        save_project("physics", path)

    message = str(caught.value)
    assert "ebs_old-token" not in message
    assert "old-refresh-secret" not in message
    assert "unevaluated-lock-contents" not in message
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sibling_lock_is_persistent_and_private_across_logout(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_saved-token", "saved-refresh", "native-client", path)
    lock_path = path.with_name(f".{path.name}.lock")
    original = lock_path.stat()

    assert logout(path) is True
    assert not path.exists()
    assert lock_path.exists()
    after_logout = lock_path.stat()
    assert (after_logout.st_dev, after_logout.st_ino) == (original.st_dev, original.st_ino)
    assert stat.S_ISREG(after_logout.st_mode)
    assert stat.S_IMODE(after_logout.st_mode) == 0o600

    save_project("physics", path)
    assert logout(path) is False
    assert read_config(path) == {"project": "physics"}
    assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == (original.st_dev, original.st_ino)


def test_unavailable_file_locking_fails_closed_without_exposing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_old-token", "old-refresh-secret", "old-client", path)
    before = path.read_bytes()
    monkeypatch.setattr(config_module, "fcntl", None)

    with pytest.raises(CLIError, match="locking is unavailable") as caught:
        save_token("ebs_new-token", path)

    assert path.read_bytes() == before
    assert "old-refresh-secret" not in str(caught.value)


def test_auth_and_project_updates_serialize_the_complete_read_modify_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    save_project("before", path)
    auth_read = threading.Event()
    project_attempted = threading.Event()
    project_read = threading.Event()
    release_auth = threading.Event()
    errors: list[BaseException] = []
    original_auth_settings = config_module._settings_for_auth_update
    original_existing_settings = config_module._existing_settings
    original_lock = config_module._config_lock

    def pause_after_auth_read(config_path: Path) -> dict[str, object]:
        settings = original_auth_settings(config_path)
        auth_read.set()
        if not release_auth.wait(2):
            raise AssertionError("test did not release the paused authentication writer")
        return settings

    def mark_project_read(config_path: Path) -> dict[str, object]:
        project_read.set()
        return original_existing_settings(config_path)

    @contextmanager
    def observe_project_lock(config_path: Path):
        if threading.current_thread().name == "project-writer":
            project_attempted.set()
        with original_lock(config_path):
            yield

    monkeypatch.setattr(config_module, "_settings_for_auth_update", pause_after_auth_read)
    monkeypatch.setattr(config_module, "_existing_settings", mark_project_read)
    monkeypatch.setattr(config_module, "_config_lock", observe_project_lock)

    def update_auth() -> None:
        try:
            save_auth_credentials("ebs_new-token", "new-refresh", "new-client", path)
        except BaseException as exc:
            errors.append(exc)

    def update_project() -> None:
        try:
            save_project("physics", path)
        except BaseException as exc:
            errors.append(exc)

    auth_thread = threading.Thread(target=update_auth)
    project_thread = threading.Thread(target=update_project, name="project-writer")
    auth_thread.start()
    assert auth_read.wait(1)
    project_thread.start()
    try:
        assert project_attempted.wait(1)
        # Project has attempted the lock but must not reach its config read
        # until the authentication writer commits and releases that same lock.
        assert not project_read.wait(0.1)
    finally:
        release_auth.set()
    auth_thread.join(2)
    project_thread.join(2)

    assert not auth_thread.is_alive()
    assert not project_thread.is_alive()
    assert errors == []
    assert project_read.is_set()
    assert saved_project(path) == "physics"
    assert load_token(path) == "ebs_new-token"
    assert load_globus_credentials(path) == ("new-refresh", "new-client")


def test_logout_waits_for_auth_update_and_cannot_resurrect_stale_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_old-token", "old-refresh", "old-client", path)
    save_project("physics", path)
    auth_read = threading.Event()
    logout_read = threading.Event()
    release_auth = threading.Event()
    errors: list[BaseException] = []
    original_read = config_module.read_config

    def pause_after_auth_read(config_path: Path) -> dict[str, object]:
        settings = original_read(config_path)
        auth_read.set()
        if not release_auth.wait(2):
            raise AssertionError("test did not release the paused authentication writer")
        return settings

    def observe_logout_read(config_path: Path | None = None) -> dict[str, object]:
        if threading.current_thread().name == "logout-writer":
            logout_read.set()
        return original_read(config_path)

    monkeypatch.setattr(config_module, "_settings_for_auth_update", pause_after_auth_read)
    monkeypatch.setattr(config_module, "read_config", observe_logout_read)

    def update_auth() -> None:
        try:
            save_auth_credentials("ebs_new-token", "new-refresh", "new-client", path)
        except BaseException as exc:
            errors.append(exc)

    def clear_auth() -> None:
        try:
            assert logout(path) is True
        except BaseException as exc:
            errors.append(exc)

    auth_thread = threading.Thread(target=update_auth, name="auth-writer")
    logout_thread = threading.Thread(target=clear_auth, name="logout-writer")
    auth_thread.start()
    assert auth_read.wait(1)
    logout_thread.start()
    try:
        # With the whole transaction locked, logout cannot perform its read
        # until the paused auth write commits and releases the sibling lock.
        assert not logout_read.wait(0.1)
    finally:
        release_auth.set()
    auth_thread.join(2)
    logout_thread.join(2)

    assert not auth_thread.is_alive()
    assert not logout_thread.is_alive()
    assert errors == []
    assert logout_read.is_set()
    assert read_config(path) == {"project": "physics"}
    assert optional_globus_credentials(path) is None


def test_expected_auth_snapshot_detects_personal_token_only_changes(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_old-token", "same-refresh", "same-client", path)
    save_project("physics", path)
    expected = read_auth_snapshot(path)
    save_auth_credentials("ebs_changed-token", "same-refresh", "same-client", path)

    with pytest.raises(CLIError, match="Concurrent login state changed"):
        save_auth_credentials("ebs_refreshed-token", "new-refresh", "same-client", path, expected=expected)
    with pytest.raises(CLIError, match="Concurrent login state changed"):
        logout(path, expected=expected)

    assert load_token(path) == "ebs_changed-token"
    assert load_globus_credentials(path) == ("same-refresh", "same-client")
    assert saved_project(path) == "physics"


def test_empty_expected_snapshot_does_not_bypass_a_new_login(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    expected = read_auth_snapshot(path)
    save_auth_credentials("ebs_other-login", "other-refresh", "other-client", path)
    save_project("physics", path)

    with pytest.raises(CLIError, match="Concurrent login state changed"):
        logout(path, expected=expected)

    assert load_token(path) == "ebs_other-login"
    assert optional_globus_credentials(path) == ("other-refresh", "other-client")
    assert saved_project(path) == "physics"


def test_malformed_snapshot_clears_only_the_same_invalid_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"token":"ebs_unfinished"', encoding="utf-8")
    path.chmod(0o600)
    original = path.read_bytes()
    expected = read_auth_snapshot(path)
    assert optional_globus_credentials(path, snapshot=expected) is None

    path.write_text('{"token":"ebs_different-unfinished"', encoding="utf-8")
    with pytest.raises(CLIError, match="Concurrent login state changed"):
        logout(path, expected=expected)
    assert path.read_bytes() != original

    expected = read_auth_snapshot(path)
    assert logout(path, expected=expected) is True
    assert not path.exists()


def test_logout_clears_both_credentials_and_preserves_project(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_auth_credentials("ebs_saved-token", "refresh-test-credential", "native-client", path)
    save_project("physics", path)

    assert logout(path) is True
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body == {"project": "physics"}
    assert logout(path) is False


def test_logout_clears_a_globus_only_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        '{"globus_refresh_token":"refresh-test","globus_client_id":"native-client"}\n',
        encoding="utf-8",
    )
    path.chmod(0o600)

    assert logout(path) is True
    assert not path.exists()


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
