from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from typer.testing import CliRunner

from ebs_cli import cli
from ebs_cli.models import CursorPage, Event, OffsetPage, S3Credentials, Trigger, UploadURL

TRIGGER_ID = UUID("12345678-1234-5678-1234-567812345678")
EVENT_ID = UUID("87654321-4321-8765-4321-876543218765")
JOB_ID = UUID("11111111-2222-3333-4444-555555555555")
SIGNED_URL = "https://s3.example.test/ebs-physics/example.fits?X-Amz-Signature=signed-secret"


def trigger_data(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(TRIGGER_ID),
        "name": "data-ready",
        "version": "v2",
        "group_id": "99999999-9999-9999-9999-999999999999",
        "group_name": "physics",
        "created_by": "alice",
        "pipeline": {"input_type": "s3_bucket_events", "matchers": {}, "outputs": {"mode": "callback"}},
        "enabled": True,
        "machine": None,
        "instance": None,
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-02T11:00:00Z",
    }
    body.update(changes)
    return body


def job_data(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(JOB_ID),
        "sfapi_job_id": "12345",
        "scheduler": "slurm",
        "status": "FAILED",
        "error": "[bold]unsafe markup[/bold]\nsecond line",
        "trigger_id": str(TRIGGER_ID),
        "event_id": str(EVENT_ID),
        "submitted_at": "2026-11-03T12:00:00Z",
        "finished_at": "2026-11-03T12:01:00Z",
        "rendered_script": "#!/bin/sh\necho do-not-render",
        "created_at": "2026-11-03T12:00:00Z",
        "updated_at": "2026-11-03T12:01:00Z",
    }
    body.update(changes)
    return body


def event_data(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(EVENT_ID),
        "trigger_id": str(TRIGGER_ID),
        "payload": {"message": "event-payload-not-shown"},
        "source": "s3",
        "status": "processed",
        "received_at": "2026-11-03T12:00:00Z",
        "job": job_data(),
    }
    body.update(changes)
    return body


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.trigger = Trigger.model_validate(trigger_data())
        self.trigger_page = OffsetPage[Trigger](items=[self.trigger], total=1, limit=50, offset=0)
        self.event_page = CursorPage[Event](
            items=[Event.model_validate(event_data())], next_cursor="next-page", has_more=True, limit=50
        )
        self.credentials = S3Credentials.model_validate(
            {
                "bucket": "ebs-physics",
                "endpoint_url": "https://s3.example.test",
                "region": "us-east-1",
                "access_key_id": "temporary-access",
                "secret_access_key": "temporary-secret",
                "session_token": "temporary-session",
                "expires_at": "2026-11-03T12:15:00Z",
            }
        )
        self.upload_url = UploadURL.model_validate(
            {
                "bucket": "ebs-physics",
                "key": "data.fits",
                "method": "PUT",
                "url": SIGNED_URL,
                "headers": {"Content-Type": "application/octet-stream"},
                "expires_at": "2026-11-03T12:15:00Z",
            }
        )

    def __enter__(self) -> "FakeAPI":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def list_triggers(self, **kwargs: Any) -> OffsetPage[Trigger]:
        self.calls.append(("list_triggers", kwargs))
        return self.trigger_page

    def get_trigger(self, trigger_id: UUID) -> Trigger:
        self.calls.append(("get_trigger", trigger_id))
        return self.trigger

    def set_trigger_enabled(self, trigger_id: UUID, enabled: bool) -> Trigger:
        self.calls.append(("set_trigger_enabled", trigger_id, enabled))
        return self.trigger

    def delete_trigger(self, trigger_id: UUID) -> None:
        self.calls.append(("delete_trigger", trigger_id))
        return None

    def list_events(self, trigger_id: UUID, **kwargs: Any) -> CursorPage[Event]:
        self.calls.append(("list_events", trigger_id, kwargs))
        return self.event_page

    def s3_credentials(self, trigger_id: UUID, **kwargs: Any) -> S3Credentials:
        self.calls.append(("s3_credentials", trigger_id, kwargs))
        return self.credentials

    def s3_upload_url(self, trigger_id: UUID, key: str, **kwargs: Any) -> UploadURL:
        self.calls.append(("s3_upload_url", trigger_id, key, kwargs))
        return self.upload_url


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EBS_TOKEN", "ebs_unit-test-token")
    monkeypatch.setenv("EBS_PROJECT", "physics")
    monkeypatch.setenv("EBS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("EBS_URL", raising=False)


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    fake = FakeAPI()
    monkeypatch.setattr(cli, "api_client", lambda ctx: fake)
    return fake
