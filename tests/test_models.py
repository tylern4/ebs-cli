from datetime import UTC, datetime

import pytest
from conftest import event_data, trigger_data
from pydantic import ValidationError

from ebs_cli.models import Event, OffsetPage, Trigger, UploadURL


def test_typed_offset_page_parses_timestamp_and_trigger() -> None:
    page = OffsetPage[Trigger].model_validate(
        {"items": [trigger_data()], "total": 1, "limit": 50, "offset": 0}
    )

    assert page.items[0].name == "data-ready"
    assert page.items[0].created_at.tzinfo == UTC


def test_missing_required_trigger_field_is_rejected() -> None:
    body = trigger_data()
    del body["pipeline"]

    with pytest.raises(ValidationError):
        Trigger.model_validate(body)


def test_event_serialization_excludes_payload_and_rendered_script() -> None:
    event = Event.model_validate(event_data(unexpected_large_payload={"do_not_print": True}))
    body = event.model_dump(mode="json")

    assert "unexpected_large_payload" not in body
    assert "payload" not in body
    assert "rendered_script" not in body["job"]


def test_upload_url_requires_absolute_http_url() -> None:
    with pytest.raises(ValidationError):
        UploadURL(
            bucket="ebs-physics",
            key="data.fits",
            method="PUT",
            url="file:///tmp/data.fits",
            headers={},
            expires_at=datetime.now(UTC),
        )


def test_upload_url_rejects_header_control_characters() -> None:
    with pytest.raises(ValidationError):
        UploadURL(
            bucket="ebs-physics",
            key="data.fits",
            method="PUT",
            url="https://s3.example.test/object?signature=valid",
            headers={"X-Test": "bad\x1bvalue"},
            expires_at=datetime.now(UTC),
        )
