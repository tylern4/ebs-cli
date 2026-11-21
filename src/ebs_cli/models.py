from datetime import datetime
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class APIModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class TriggerInstance(APIModel):
    id: UUID | None = None
    name: str | None = None
    status: str
    failure_reason: str | None = None


class Trigger(APIModel):
    id: UUID
    name: str
    version: str | None = None
    group_id: UUID
    group_name: str | None = None
    created_by: str
    pipeline: dict[str, Any]
    enabled: bool
    machine: str | None = None
    instance: TriggerInstance | None = None
    created_at: datetime
    updated_at: datetime


class Job(APIModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    sfapi_job_id: str | None = None
    scheduler: str
    status: str
    error: str | None = None
    trigger_id: UUID
    event_id: UUID | None = None
    submitted_at: datetime | None = None
    finished_at: datetime | None = None
    rendered_script: str | None = Field(default=None, exclude=True, repr=False)
    created_at: datetime
    updated_at: datetime


class Event(APIModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    trigger_id: UUID
    payload: dict[str, Any] = Field(default_factory=dict, exclude=True, repr=False)
    source: str
    status: str
    received_at: datetime
    job: Job | None = None


T = TypeVar("T")


class OffsetPage(BaseModel, Generic[T]):
    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool
    limit: int = Field(ge=1)


class S3Credentials(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    region: str = Field(min_length=1)
    access_key_id: str = Field(min_length=1, repr=False)
    secret_access_key: str = Field(min_length=1, repr=False)
    session_token: str = Field(min_length=1, repr=False)  # noqa: B105
    expires_at: datetime
    # Core's echo of the requested capability; the default keeps parsing
    # tolerant of a backend that predates the echo.
    operation: Literal["upload", "list", "download", "delete"] = "upload"

    @field_validator(
        "bucket", "endpoint_url", "region", "access_key_id", "secret_access_key", "session_token"
    )
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(not character.isprintable() for character in value):
            raise ValueError("credential fields cannot contain control characters")
        return value


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    # Present when the API supported the opt-in set-based count. Older APIs
    # omit it and the CLI retains its per-Project compatibility fallback.
    trigger_count: int | None = Field(default=None, ge=0)

    @field_validator("name", "role")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(not character.isprintable() for character in value):
            raise ValueError("project fields cannot contain control characters")
        return value


class ProjectPage(BaseModel):
    items: list[Project]


class UploadURL(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket: str = Field(min_length=1)
    key: str = Field(min_length=1)
    method: Literal["PUT"]
    url: str = Field(min_length=1, repr=False)
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header_value in value.items():
            if not name or any(not character.isprintable() for character in name + header_value):
                raise ValueError("upload headers cannot contain control characters")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("upload URL must be an absolute HTTP or HTTPS URL")
        if any(not character.isprintable() or character.isspace() for character in value):
            raise ValueError("upload URL cannot contain whitespace or control characters")
        return value
