from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from conftest import FakeAPI

from ebs_cli.errors import CLIError
from ebs_cli.uploads import upload_file


def test_boto3_client_uses_scoped_credentials_sigv4_path_style_and_timeouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credentials = FakeAPI().credentials
    path = tmp_path / "large-input.fits"
    path.write_bytes(b"local-test-data")
    s3_client = Mock()
    factory = Mock(return_value=s3_client)
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", factory)

    upload_file(credentials, path, "multipart/run.fits")

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
    s3_client.upload_file.assert_called_once_with(str(path), "ebs-physics", "multipart/run.fits")


def test_boto3_service_failure_does_not_expose_temporary_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credentials = FakeAPI().credentials
    path = tmp_path / "data.fits"
    path.write_bytes(b"local-test-data")
    s3_client = Mock()
    s3_client.upload_file.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": "temporary-access temporary-secret temporary-session must not be repeated",
            }
        },
        "PutObject",
    )
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=s3_client))

    with pytest.raises(CLIError) as caught:
        upload_file(credentials, path, "data.fits")

    message = str(caught.value)
    assert "S3 upload failed" in message
    assert "temporary-access" not in message
    assert "temporary-secret" not in message
    assert "temporary-session" not in message
    assert "AccessDenied" not in message


def test_boto3_connection_failure_has_safe_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credentials = FakeAPI().credentials
    path = tmp_path / "data.fits"
    path.write_bytes(b"local-test-data")
    factory = Mock(side_effect=EndpointConnectionError(endpoint_url=credentials.endpoint_url))
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", factory)

    with pytest.raises(CLIError, match="Check the file and network"):
        upload_file(credentials, path, "data.fits")


def test_managed_upload_failure_does_not_return_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    credentials = FakeAPI().credentials
    path = tmp_path / "data.fits"
    path.write_bytes(b"local-test-data")
    s3_client = Mock()
    s3_client.upload_file.side_effect = OSError("source changed during multipart upload")
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=s3_client))

    with pytest.raises(CLIError) as caught:
        upload_file(credentials, path, "data.fits")

    assert "uploaded" not in str(caught.value).lower()
