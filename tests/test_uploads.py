from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from conftest import SIGNED_URL, FakeAPI
from rich.console import Console

from ebs_cli.errors import CLIError
from ebs_cli.uploads import upload_file


class ManagedUploadTransport:
    """Read in bounded chunks and emulate boto3's managed-upload callback."""

    def __init__(self, *, chunk_size: int = 4, rewind_after_bytes: int | None = None) -> None:
        self.chunk_size = chunk_size
        self.rewind_after_bytes = rewind_after_bytes
        self.rewound = False
        self.callback = None
        self.callback_deltas: list[int] = []
        self.received_bytes = 0

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        ExtraArgs: object = None,
        Callback: object = None,
        Config: object = None,
    ) -> None:
        assert (bucket, key) == ("ebs-physics", "run.fits")
        assert ExtraArgs is None and Config is None
        self.callback = Callback
        with open(filename, "rb") as source:
            while chunk := source.read(self.chunk_size):
                if not self.rewound and self.rewind_after_bytes == self.received_bytes:
                    # s3transfer rewinds a failed part with a negative delta.
                    self.rewound = True
                    rewound_bytes = self.received_bytes
                    self._report(-rewound_bytes)
                    self._report(rewound_bytes)
                self._report(len(chunk))

    def _report(self, amount: int) -> None:
        self.callback_deltas.append(amount)
        self.received_bytes += amount
        if self.callback is not None:
            self.callback(amount)


def terminal_console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=True, width=100), output


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


def test_interactive_upload_reports_chunks_retries_and_completed_total(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "test.fits"
    path.write_bytes(b"twelve bytes")
    transport = ManagedUploadTransport(rewind_after_bytes=4)
    console, output = terminal_console()
    monkeypatch.setattr("ebs_cli.uploads.error_console", console)
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=transport))

    upload_file(FakeAPI().credentials, path, "run.fits")

    assert transport.callback is not None
    assert transport.callback_deltas == [4, -4, 4, 4, 4]
    assert transport.received_bytes == 12
    rendered = output.getvalue()
    assert "Uploading test.fits" in rendered
    assert "100%" in rendered
    assert "12/12 bytes" in rendered
    assert rendered.endswith("\n\x1b[?25h")


def test_noninteractive_upload_has_clean_output_and_no_progress_callback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "test.fits"
    path.write_bytes(b"small")
    transport = ManagedUploadTransport(chunk_size=2)
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    monkeypatch.setattr("ebs_cli.uploads.error_console", console)
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=transport))

    upload_file(FakeAPI().credentials, path, "run.fits")

    assert transport.callback is None
    assert transport.callback_deltas == [2, 2, 1]
    assert transport.received_bytes == 5
    assert output.getvalue() == ""


def test_sparse_1_5gb_upload_is_read_in_bounded_chunks_and_reports_total(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "sparse.fits"
    size = 1_500_000_000
    with path.open("wb") as source:
        source.truncate(size)
    transport = ManagedUploadTransport(chunk_size=8 * 1024 * 1024, rewind_after_bytes=8 * 1024 * 1024)
    console, output = terminal_console()
    monkeypatch.setattr("ebs_cli.uploads.error_console", console)
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=transport))

    upload_file(FakeAPI().credentials, path, "run.fits")

    assert path.stat().st_size == size
    assert transport.received_bytes == size
    assert transport.callback_deltas[0:4] == [
        8 * 1024 * 1024,
        -8 * 1024 * 1024,
        8 * 1024 * 1024,
        8 * 1024 * 1024,
    ]
    assert len(transport.callback_deltas) == 181
    assert "100%" in output.getvalue()
    assert "1.5/1.5 GB" in output.getvalue()


def test_interactive_transfer_failure_keeps_safe_error_and_secret_free_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "failure.fits"
    path.write_bytes(b"partial data")
    console, output = terminal_console()

    def fail_after_chunk(filename: str, bucket: str, key: str, **options: object) -> None:
        assert (bucket, key) == ("ebs-physics", "run.fits")
        with open(filename, "rb") as source:
            options["Callback"](len(source.read(4)))
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": (
                        f"temporary-access temporary-secret temporary-session Authorization {SIGNED_URL}"
                    ),
                }
            },
            "PutObject",
        )

    monkeypatch.setattr("ebs_cli.uploads.error_console", console)
    monkeypatch.setattr("ebs_cli.s3ops.boto3.client", Mock(return_value=Mock(upload_file=fail_after_chunk)))

    with pytest.raises(CLIError) as caught:
        upload_file(FakeAPI().credentials, path, "run.fits")

    for secret in ("temporary-access", "temporary-secret", "temporary-session", SIGNED_URL, "Authorization"):
        assert secret not in str(caught.value)
        assert secret not in output.getvalue()
    assert "S3 upload failed" in str(caught.value)
    assert "100%" not in output.getvalue()
    assert "Uploaded" not in output.getvalue()
