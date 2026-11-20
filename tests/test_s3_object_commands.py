import json
import os
import stat
from datetime import UTC
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from conftest import TRIGGER_ID, FakeAPI
from typer.testing import CliRunner

from ebs_cli.cli import app
from ebs_cli.errors import CLIError
from ebs_cli.models import S3Credentials
from ebs_cli.s3ops import DOWNLOAD_CHUNK_BYTES, delete_object, download_object, list_objects

SECRETS = ("temporary-access", "temporary-secret", "temporary-session")

LIST_ITEMS = [
    {"key": "run/a.fits", "size": 10, "last_modified": "2026-11-03T12:00:00Z"},
    {"key": "run/b.fits", "size": 20, "last_modified": "2026-11-03T12:01:00Z"},
]


def exchange_call(operation: str) -> tuple[str, object, dict[str, object]]:
    return ("s3_credentials", TRIGGER_ID, {"duration_seconds": 900, "operation": operation})


# --- ebs s3 ls ---------------------------------------------------------------


def test_ls_requests_list_operation_only_and_bounded_page(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = Mock(return_value={"items": LIST_ITEMS, "next_cursor": None})
    monkeypatch.setattr("ebs_cli.cli.list_objects", listed)

    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID), "--prefix", "run/", "--limit", "5"])

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [exchange_call("list")]
    listed.assert_called_once_with(fake_api.credentials, prefix="run/", limit=5, cursor=None)


def test_ls_json_is_stable_and_includes_token_only_when_returned(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ebs_cli.cli.list_objects", Mock(return_value={"items": LIST_ITEMS, "next_cursor": "tok-1"})
    )

    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID), "--limit", "5", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "bucket": "ebs-physics",
        "limit": 5,
        "prefix": "",
        "items": LIST_ITEMS,
        "next_cursor": "tok-1",
    }
    for secret in SECRETS:
        assert secret not in result.output


def test_ls_json_omits_token_when_s3_returns_none(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ebs_cli.cli.list_objects", Mock(return_value={"items": [], "next_cursor": None}))

    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID), "--json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["items"] == []
    assert "next_cursor" not in body


def test_ls_human_output_shows_key_size_time_and_continuation_hint(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ebs_cli.cli.list_objects", Mock(return_value={"items": LIST_ITEMS, "next_cursor": "tok-1"})
    )

    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID)], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "run/a.fits" in result.stdout
    assert "10" in result.stdout
    assert "2026-11-03T12:00:00Z" in result.stdout
    assert "--cursor tok-1" in result.stdout
    for secret in SECRETS:
        assert secret not in result.output


def test_ls_human_output_without_more_results(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ebs_cli.cli.list_objects", Mock(return_value={"items": LIST_ITEMS, "next_cursor": None})
    )

    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID)], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "No more results" in result.stdout
    assert "--cursor" not in result.stdout


def test_ls_human_output_empty_listing(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ebs_cli.cli.list_objects", Mock(return_value={"items": [], "next_cursor": None}))

    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID)])

    assert result.exit_code == 0, result.output
    assert "No objects matched this listing." in result.stdout


def test_ls_forwards_cursor_token(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = Mock(return_value={"items": [], "next_cursor": None})
    monkeypatch.setattr("ebs_cli.cli.list_objects", listed)

    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID), "--cursor", "opaque-token"])

    assert result.exit_code == 0, result.output
    listed.assert_called_once_with(fake_api.credentials, prefix="", limit=100, cursor="opaque-token")


@pytest.mark.parametrize("limit", ["0", "1001", "-1"])
def test_ls_rejects_limits_outside_bounds_before_exchange(
    runner: CliRunner, fake_api: FakeAPI, limit: str
) -> None:
    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID), "--limit", limit])

    assert result.exit_code == 2
    assert fake_api.calls == []


def test_ls_rejects_invalid_cursor_before_exchange(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID), "--cursor", "bad\ncursor"])

    assert result.exit_code == 2
    assert fake_api.calls == []


def test_ls_rejects_invalid_prefix_before_exchange(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "ls", str(TRIGGER_ID), "--prefix", "bad\nprefix"])

    assert result.exit_code == 2
    assert fake_api.calls == []


# --- ebs s3 download ---------------------------------------------------------


def test_download_requests_download_operation_and_streams_to_destination(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    downloaded = Mock(return_value=None)
    monkeypatch.setattr("ebs_cli.cli.download_object", downloaded)

    result = runner.invoke(app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(destination)])

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [exchange_call("download")]
    downloaded.assert_called_once_with(fake_api.credentials, "run/a.fits", destination, force=False)
    assert f"Downloaded s3://ebs-physics/run/a.fits to {destination}" in result.stdout
    for secret in SECRETS:
        assert secret not in result.output


def test_download_json_contains_only_target_and_destination(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    monkeypatch.setattr("ebs_cli.cli.download_object", Mock(return_value=None))

    result = runner.invoke(app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(destination), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "bucket": "ebs-physics",
        "key": "run/a.fits",
        "path": str(destination),
        "status": "downloaded",
        "target": "s3://ebs-physics/run/a.fits",
    }
    assert "url" not in result.stdout
    for secret in SECRETS:
        assert secret not in result.output


def test_download_refuses_existing_destination_without_force(
    runner: CliRunner, fake_api: FakeAPI, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    destination.write_bytes(b"original")

    result = runner.invoke(app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(destination)])

    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert fake_api.calls == []
    assert destination.read_bytes() == b"original"


def test_download_force_overwrites_existing_destination(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    destination.write_bytes(b"original")
    downloaded = Mock(return_value=None)
    monkeypatch.setattr("ebs_cli.cli.download_object", downloaded)

    result = runner.invoke(
        app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(destination), "--force"]
    )

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [exchange_call("download")]
    downloaded.assert_called_once_with(fake_api.credentials, "run/a.fits", destination, force=True)


def test_download_refuses_symlink_destination_even_with_force(
    runner: CliRunner, fake_api: FakeAPI, tmp_path: Path
) -> None:
    target = tmp_path / "precious.fits"
    target.write_bytes(b"keep")
    link = tmp_path / "link.fits"
    link.symlink_to(target)

    for extra in ([], ["--force"]):
        result = runner.invoke(app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(link), *extra])

        assert result.exit_code == 2, result.output
        assert "symbolic link" in result.stderr
        assert fake_api.calls == []
        assert target.read_bytes() == b"keep"


def test_download_refuses_missing_destination_directory(
    runner: CliRunner, fake_api: FakeAPI, tmp_path: Path
) -> None:
    destination = tmp_path / "missing-dir" / "a.fits"

    result = runner.invoke(app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(destination)])

    assert result.exit_code == 2
    assert "Destination directory does not exist" in result.stderr
    assert fake_api.calls == []


def test_download_rejects_directory_destination(runner: CliRunner, fake_api: FakeAPI, tmp_path: Path) -> None:
    destination = tmp_path / "into-dir"
    destination.mkdir()

    result = runner.invoke(
        app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(destination), "--force"]
    )

    assert result.exit_code == 2
    assert "directory" in result.stderr
    assert fake_api.calls == []


def test_download_rejects_invalid_key_before_exchange(
    runner: CliRunner, fake_api: FakeAPI, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["s3", "download", str(TRIGGER_ID), "../escape", str(tmp_path / "a.fits")])

    assert result.exit_code == 2
    assert fake_api.calls == []


def test_download_failure_has_no_success_output_or_secrets(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"

    def fail(*args: object, **kwargs: object) -> None:
        raise CLIError("S3 download failed. Request new temporary credentials and retry.")

    monkeypatch.setattr("ebs_cli.cli.download_object", fail)

    result = runner.invoke(app, ["s3", "download", str(TRIGGER_ID), "run/a.fits", str(destination), "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "S3 download failed" in result.stderr
    for secret in SECRETS:
        assert secret not in result.output
    assert not destination.exists()


# --- ebs s3 rm ---------------------------------------------------------------


def test_rm_with_yes_requests_delete_operation_and_deletes_one_object(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted = Mock(return_value=None)
    monkeypatch.setattr("ebs_cli.cli.delete_object", deleted)

    result = runner.invoke(app, ["s3", "rm", str(TRIGGER_ID), "run/a.fits", "--yes"])

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [exchange_call("delete")]
    deleted.assert_called_once_with(fake_api.credentials, "run/a.fits")
    assert "Deleted s3://ebs-physics/run/a.fits." in result.stdout
    for secret in SECRETS:
        assert secret not in result.output


def test_rm_json_contains_only_target_and_deleted_status(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ebs_cli.cli.delete_object", Mock(return_value=None))

    result = runner.invoke(app, ["s3", "rm", str(TRIGGER_ID), "run/a.fits", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "bucket": "ebs-physics",
        "key": "run/a.fits",
        "status": "deleted",
        "target": "s3://ebs-physics/run/a.fits",
    }


def test_rm_noninteractive_requires_yes_before_exchange(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "rm", str(TRIGGER_ID), "run/a.fits"])

    assert result.exit_code == 2
    assert "--yes" in result.stderr
    assert fake_api.calls == []


def test_rm_interactive_confirmation_names_target_and_can_be_declined(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ebs_cli.cli._stdin_is_interactive", lambda: True)

    result = runner.invoke(app, ["s3", "rm", str(TRIGGER_ID), "run/a.fits"], input="n\n")

    assert result.exit_code != 0
    assert "Permanently delete" in result.stdout
    assert "run/a.fits" in result.stdout
    assert str(TRIGGER_ID) in result.stdout
    assert fake_api.calls == []


def test_rm_interactive_confirmation_acceptance_proceeds(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ebs_cli.cli._stdin_is_interactive", lambda: True)
    deleted = Mock(return_value=None)
    monkeypatch.setattr("ebs_cli.cli.delete_object", deleted)

    result = runner.invoke(app, ["s3", "rm", str(TRIGGER_ID), "run/a.fits"], input="y\n")

    assert result.exit_code == 0, result.output
    assert fake_api.calls == [exchange_call("delete")]
    deleted.assert_called_once_with(fake_api.credentials, "run/a.fits")


def test_rm_rejects_invalid_key_before_exchange(runner: CliRunner, fake_api: FakeAPI) -> None:
    result = runner.invoke(app, ["s3", "rm", str(TRIGGER_ID), "../escape", "--yes"])

    assert result.exit_code == 2
    assert fake_api.calls == []


def test_rm_failure_has_no_success_output_or_secrets(
    runner: CliRunner, fake_api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        raise CLIError("S3 delete failed. The object may still exist.")

    monkeypatch.setattr("ebs_cli.cli.delete_object", fail)

    result = runner.invoke(app, ["s3", "rm", str(TRIGGER_ID), "run/a.fits", "--yes", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "S3 delete failed" in result.stderr
    for secret in SECRETS:
        assert secret not in result.output


# --- s3ops unit behavior -----------------------------------------------------


def test_s3ops_list_uses_exact_bounded_kwargs(monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI) -> None:
    client = Mock()
    client.list_objects_v2.return_value = {
        "Contents": [{"Key": "run/a.fits", "Size": 10, "LastModified": "x"}],
        "NextContinuationToken": "next-token",
    }
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    page = list_objects(fake_api.credentials, prefix="run/", limit=7, cursor="tok")

    client.list_objects_v2.assert_called_once_with(
        Bucket="ebs-physics", MaxKeys=7, Prefix="run/", ContinuationToken="tok"
    )
    assert page == {
        "items": [{"key": "run/a.fits", "size": 10, "last_modified": ""}],
        "next_cursor": "next-token",
    }


def test_s3ops_list_omits_empty_prefix_and_missing_cursor(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    client = Mock()
    client.list_objects_v2.return_value = {}
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    page = list_objects(fake_api.credentials, prefix="", limit=100, cursor=None)

    client.list_objects_v2.assert_called_once_with(Bucket="ebs-physics", MaxKeys=100)
    assert page == {"items": [], "next_cursor": None}


def test_s3ops_list_renders_utc_timestamps_and_skips_unusable_entries(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    from datetime import datetime

    client = Mock()
    client.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "a.fits", "Size": 5, "LastModified": datetime(2026, 11, 3, 12, 0, tzinfo=UTC)},
            {"Key": "naive.fits", "Size": 6, "LastModified": datetime(2026, 11, 3, 12, 0)},
            {"Key": "bad.fits", "Size": "not-a-number", "LastModified": None},
            {"Size": 7},
            "garbage",
        ]
    }
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    page = list_objects(fake_api.credentials, prefix="", limit=10, cursor=None)

    assert page["items"][0]["last_modified"] == "2026-11-03T12:00:00Z"
    assert page["items"][1]["last_modified"] == "2026-11-03T12:00:00Z"
    assert page["items"][2] == {"key": "bad.fits", "size": 0, "last_modified": ""}
    assert len(page["items"]) == 3


def test_s3ops_list_failure_is_safe_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    client = Mock()
    client.list_objects_v2.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": f"{SECRETS[0]} {SECRETS[1]} {SECRETS[2]} must not be repeated",
            }
        },
        "ListObjectsV2",
    )
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    with pytest.raises(CLIError) as caught:
        list_objects(fake_api.credentials, prefix="", limit=10, cursor=None)

    message = str(caught.value)
    assert "S3 listing failed" in message
    for secret in SECRETS:
        assert secret not in message
    assert "AccessDenied" not in message


class _StreamingBody:
    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    def iter_chunks(self, chunk_size: int) -> object:
        assert chunk_size == DOWNLOAD_CHUNK_BYTES
        return iter(self._chunks)


def test_s3ops_download_streams_to_owner_only_temp_file_and_renames_atomically(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    client = Mock()
    client.get_object.return_value = {"Body": _StreamingBody([b"chunk-one", b"chunk-two"])}
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))
    replaced: list[tuple[Path, Path, int]] = []
    real_replace = os.replace

    def spy_replace(source: str, destination_path: str) -> None:
        info = os.stat(source)
        replaced.append((Path(source), Path(destination_path), stat.S_IMODE(info.st_mode)))
        real_replace(source, destination_path)

    monkeypatch.setattr("ebs_cli.s3ops.os.replace", spy_replace)

    download_object(fake_api.credentials, "run/a.fits", destination)

    client.get_object.assert_called_once_with(Bucket="ebs-physics", Key="run/a.fits")
    source, target, mode = replaced[0]
    assert target == destination
    assert source.parent == destination.parent
    assert source.name.startswith(".a.fits.") and source.name.endswith(".part")
    assert mode == 0o600
    assert destination.read_bytes() == b"chunk-onechunk-two"
    assert [path.name for path in tmp_path.iterdir()] == ["a.fits"]


def test_s3ops_download_cleans_temporary_file_on_midstream_failure(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"

    class _FailingBody:
        def iter_chunks(self, chunk_size: int) -> object:
            yield b"partial"
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": SECRETS[1]}}, "GetObject")

    client = Mock()
    client.get_object.return_value = {"Body": _FailingBody()}
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    with pytest.raises(CLIError) as caught:
        download_object(fake_api.credentials, "run/a.fits", destination)

    message = str(caught.value)
    assert "S3 download failed" in message
    for secret in SECRETS:
        assert secret not in message
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_s3ops_download_rechecks_absent_destination_before_rename(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    client = Mock()

    def create_during_fetch(**request: object) -> object:
        assert request == {"Bucket": "ebs-physics", "Key": "run/a.fits"}
        destination.write_bytes(b"another process owns this now")
        return {"Body": _StreamingBody([b"downloaded data"])}

    client.get_object.side_effect = create_during_fetch
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    with pytest.raises(CLIError, match="S3 download failed"):
        download_object(fake_api.credentials, "run/a.fits", destination)

    assert destination.read_bytes() == b"another process owns this now"
    assert list(tmp_path.iterdir()) == [destination]


def test_s3ops_download_rechecks_symlink_before_rename_even_with_force(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    protected = tmp_path / "protected.fits"
    protected.write_bytes(b"preserve")
    client = Mock()

    def link_during_fetch(**request: object) -> object:
        destination.symlink_to(protected)
        return {"Body": _StreamingBody([b"downloaded data"])}

    client.get_object.side_effect = link_during_fetch
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    with pytest.raises(CLIError, match="S3 download failed"):
        download_object(fake_api.credentials, "run/a.fits", destination, force=True)

    assert destination.is_symlink()
    assert protected.read_bytes() == b"preserve"
    assert set(tmp_path.iterdir()) == {destination, protected}


def test_s3ops_download_cleans_temporary_file_when_get_object_fails(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, tmp_path: Path
) -> None:
    destination = tmp_path / "a.fits"
    client = Mock()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": SECRETS[2]}}, "GetObject"
    )
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    with pytest.raises(CLIError) as caught:
        download_object(fake_api.credentials, "run/a.fits", destination)

    assert "S3 download failed" in str(caught.value)
    assert SECRETS[2] not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_s3ops_download_temporary_file_creation_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI, tmp_path: Path
) -> None:
    def fail_mkstemp(*args: object, **kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr("ebs_cli.s3ops.tempfile.mkstemp", fail_mkstemp)

    with pytest.raises(CLIError) as caught:
        download_object(fake_api.credentials, "run/a.fits", tmp_path / "a.fits")

    assert "Could not create a temporary file" in str(caught.value)
    assert "disk full" not in str(caught.value)


def test_s3ops_delete_calls_delete_object_once_with_derived_bucket(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    client = Mock()
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    delete_object(fake_api.credentials, "run/a.fits")

    client.delete_object.assert_called_once_with(Bucket="ebs-physics", Key="run/a.fits")


def test_s3ops_delete_failure_is_safe_and_secret_free(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    client = Mock()
    client.delete_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": SECRETS[0]}}, "DeleteObject"
    )
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=client))

    with pytest.raises(CLIError) as caught:
        delete_object(fake_api.credentials, "run/a.fits")

    message = str(caught.value)
    assert "S3 delete failed" in message
    for secret in SECRETS:
        assert secret not in message


def test_s3ops_client_construction_matches_upload_contract(
    monkeypatch: pytest.MonkeyPatch, fake_api: FakeAPI
) -> None:
    factory = Mock()
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", factory)
    credentials: S3Credentials = fake_api.credentials

    from ebs_cli.s3ops import s3_client

    s3_client(credentials)

    factory.assert_called_once()
    assert factory.call_args.args == ("s3",)
    arguments = factory.call_args.kwargs
    assert arguments["endpoint_url"] == "https://s3.example.test"
    assert arguments["region_name"] == "us-east-1"
    assert arguments["aws_access_key_id"] == "temporary-access"
    assert arguments["aws_secret_access_key"] == "temporary-secret"
    assert arguments["aws_session_token"] == "temporary-session"
    config = arguments["config"]
    assert config.signature_version == "s3v4"
    assert config.s3 == {"addressing_style": "path"}
    assert config.connect_timeout == 10
    assert config.read_timeout == 120
    assert config.retries == {"max_attempts": 3, "mode": "standard"}
