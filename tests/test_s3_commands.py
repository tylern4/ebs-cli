import json
from pathlib import Path

import pytest
from conftest import SIGNED_URL, TRIGGER_ID, FakeAPI
from typer.testing import CliRunner

from ebs_cli.cli import app


def test_credentials_json_contains_all_temporary_values_only_on_stdout(
    runner: CliRunner, fake_api: FakeAPI
) -> None:
    result = runner.invoke(
        app, ["s3", "credentials", str(TRIGGER_ID), "--duration-seconds", "1200", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    body = json.loads(result.stdout)
    assert body["access_key_id"] == "temporary-access"
    assert body["secret_access_key"] == "temporary-secret"
    assert body["session_token"] == "temporary-session"
    assert body["bucket"] == "ebs-physics"
    assert fake_api.calls == [
        ("s3_credentials", TRIGGER_ID, {"duration_seconds": 1200, "operation": "upload"})
    ]


def test_credentials_shell_exports_all_temporary_values(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "credentials", str(TRIGGER_ID), "--shell"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert "AWS_ACCESS_KEY_ID=temporary-access" in result.stdout
    assert "AWS_SECRET_ACCESS_KEY=temporary-secret" in result.stdout
    assert "AWS_SESSION_TOKEN=temporary-session" in result.stdout
    assert "AWS_DEFAULT_REGION=us-east-1" in result.stdout
    assert "EBS_S3_ENDPOINT=https://s3.example.test" in result.stdout
    assert "boto3.client" not in result.stdout


def test_credentials_human_output_is_a_complete_boto3_example(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "credentials", str(TRIGGER_ID)], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    for value in (
        "temporary-access",
        "temporary-secret",
        "temporary-session",
        "https://s3.example.test",
        "us-east-1",
        "boto3.client",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "signature_version",
        "s3v4",
        "addressing_style",
        "path",
        "connect_timeout",
        "read_timeout",
        "upload_file",
    ):
        assert value in result.stdout


def test_credentials_rejects_conflicting_output_options(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "credentials", str(TRIGGER_ID), "--json", "--shell"])

    assert result.exit_code == 2
    assert fake_api.calls == []


def test_upload_url_is_redacted_by_default(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "upload-url", str(TRIGGER_ID), "data.fits", "--json"])

    assert result.exit_code == 0, result.output
    assert SIGNED_URL not in result.stdout
    assert "signed-secret" not in result.stdout
    assert json.loads(result.stdout)["url"] == "[signed URL redacted]"


def test_upload_url_human_output_has_put_expiry_and_redaction_notice(
    runner: CliRunner, fake_api: FakeAPI
) -> None:
    result = runner.invoke(app, ["s3", "upload-url", str(TRIGGER_ID), "data.fits"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "curl" in result.stdout
    assert "PUT" in result.stdout
    assert "Expires" in result.stdout
    assert "--show-url" in result.stdout
    assert SIGNED_URL not in result.stdout


def test_upload_url_can_be_intentionally_revealed_in_json(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "upload-url", str(TRIGGER_ID), "data.fits", "--json", "--show-url"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["url"] == SIGNED_URL
    assert fake_api.calls[-1] == (
        "s3_upload_url",
        TRIGGER_ID,
        "data.fits",
        {"duration_seconds": 900},
    )


def test_direct_upload_uses_credentials_and_hides_every_temporary_value(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "local-data.fits"
    path.write_bytes(b"fits")
    uploaded = []
    monkeypatch.setattr(
        "ebs_cli.cli.upload_file",
        lambda credentials, file, key: uploaded.append((credentials, file, key)),
    )

    result = runner.invoke(app, ["s3", "upload", str(TRIGGER_ID), str(path)])

    assert result.exit_code == 0, result.output
    assert "s3://ebs-physics/local-data.fits" in result.stdout
    assert fake_api.calls == [
        ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": "upload"})
    ]
    assert uploaded == [(fake_api.credentials, path, "local-data.fits")]
    for secret in ("temporary-access", "temporary-secret", "temporary-session", SIGNED_URL):
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_direct_upload_json_contains_only_target_and_success(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "input.fits"
    path.write_bytes(b"fits")
    monkeypatch.setattr("ebs_cli.cli.upload_file", lambda credentials, file, key: None)

    result = runner.invoke(
        app,
        ["s3", "upload", str(TRIGGER_ID), str(path), "--key", "folder/output.fits", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "bucket": "ebs-physics",
        "key": "folder/output.fits",
        "status": "uploaded",
        "target": "s3://ebs-physics/folder/output.fits",
    }
    assert "url" not in result.stdout
    assert fake_api.calls == [
        ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": "upload"})
    ]
    for secret in ("temporary-access", "temporary-secret", "temporary-session"):
        assert secret not in result.stdout


def test_direct_upload_s3_failure_has_no_success_json_or_secrets(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ebs_cli.errors import CLIError

    path = tmp_path / "input.fits"
    path.write_bytes(b"fits")

    def fail_upload(credentials, file, key):
        raise CLIError("S3 upload failed. Request new temporary credentials and retry.")

    monkeypatch.setattr("ebs_cli.cli.upload_file", fail_upload)

    result = runner.invoke(app, ["s3", "upload", str(TRIGGER_ID), str(path), "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "S3 upload failed" in result.stderr
    for secret in ("temporary-access", "temporary-secret", "temporary-session"):
        assert secret not in result.output


def test_missing_file_fails_before_requesting_credentials(
    runner: CliRunner, fake_api: FakeAPI, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.fits"

    result = runner.invoke(app, ["s3", "upload", str(TRIGGER_ID), str(missing)])

    assert result.exit_code == 2
    assert "does not exist" in result.stderr
    assert fake_api.calls == []


def test_invalid_key_fails_before_requesting_signed_url(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "upload-url", str(TRIGGER_ID), "../escape"])

    assert result.exit_code == 2
    assert fake_api.calls == []
    assert "Object key" in result.stderr


OPERATION_EXAMPLES = [
    ("upload", "upload_file", ("list_objects_v2", "get_object", "delete_object")),
    ("list", "list_objects_v2", ("upload_file", "get_object", "delete_object")),
    ("download", "get_object", ("upload_file", "list_objects_v2", "delete_object")),
    ("delete", "delete_object", ("upload_file", "list_objects_v2", "get_object")),
]


@pytest.mark.parametrize(("operation", "expected", "absent"), OPERATION_EXAMPLES)
def test_credentials_example_matches_selected_operation(
    runner: CliRunner, fake_api: FakeAPI, operation: str, expected: str, absent: tuple[str, ...]
) -> None:
    result = runner.invoke(
        app,
        ["s3", "credentials", str(TRIGGER_ID), "--operation", operation],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.output
    assert expected in result.stdout
    for other in absent:
        assert other not in result.stdout
    assert fake_api.calls == [
        ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": operation})
    ]
    # Unlike the direct object commands, `s3 credentials` intentionally prints
    # the temporary values in its human-readable example.


def test_credentials_rejects_unknown_operation(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "credentials", str(TRIGGER_ID), "--operation", "fetch"])

    assert result.exit_code == 2
    assert fake_api.calls == []
