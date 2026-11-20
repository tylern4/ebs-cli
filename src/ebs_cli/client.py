from types import TracebackType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import TypeAdapter, ValidationError

from ebs_cli.errors import CLIError
from ebs_cli.models import (
    CursorPage,
    Event,
    OffsetPage,
    Project,
    ProjectPage,
    S3Credentials,
    Trigger,
    UploadURL,
)
from ebs_cli.output import safe_text
from ebs_cli.security import redact_secrets

API_PREFIX = "/api/v1"


def normalize_server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise CLIError(
            "Server URL must be an HTTP or HTTPS origin without credentials, paths, or queries.", exit_code=2
        )
    return value


def validate_object_key(value: str) -> str:
    if not value or len(value.encode("utf-8")) > 1024:
        raise CLIError("Object key must contain 1 to 1024 UTF-8 bytes.", exit_code=2)
    if any(not character.isprintable() for character in value) or "\\" in value:
        raise CLIError(
            "Object key cannot be empty or contain control characters or backslashes.", exit_code=2
        )
    if value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")):
        raise CLIError(
            "Object key must be relative and cannot contain empty, '.' or '..' segments.", exit_code=2
        )
    return value


class EBSClient:
    """Typed synchronous client for the user API."""

    def __init__(
        self,
        url: str,
        token: str,
        project: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: httpx.Timeout | float = 30.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if project is not None:
            headers["X-Group"] = project
        self._http = httpx.Client(
            base_url=normalize_server_url(url),
            headers=headers,
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "EBSClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        sensitive: bool = False,
        project_scoped: bool = True,
    ) -> Any:
        try:
            response = self._http.request(method, f"{API_PREFIX}{path}", params=params, json=json)
        except httpx.TimeoutException as exc:
            raise CLIError("Request timed out. Check the server URL and retry.") from exc
        except httpx.RequestError as exc:
            raise CLIError(
                "Could not reach the EBS server. Check the server URL and network connection."
            ) from exc

        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:
                raise CLIError("Server returned an invalid JSON response.") from exc

        if sensitive:
            detail = "The server did not return details for this secrets-bearing request."
        else:
            detail = _response_detail(response)
        if response.status_code == 401:
            message = "Authentication failed. Run 'ebs auth login' or update EBS_TOKEN."
        elif response.status_code == 403:
            if project_scoped:
                message = f"Access denied for the selected Project. {detail}"
            else:
                message = f"Access denied. {detail}"
        elif response.status_code == 404:
            message = f"Requested EBS resource was not found. {detail}"
        elif response.status_code in {301, 302, 303, 307, 308}:
            message = "Server attempted a redirect. Check the configured server URL; redirects are disabled."
        elif response.status_code >= 500:
            message = (
                f"EBS server failed with HTTP {response.status_code}. "
                f"Retry later or contact support. {detail}"
            )
        else:
            message = f"Request failed with HTTP {response.status_code}. {detail}"
        raise CLIError(redact_secrets(safe_text(message, limit=700)))

    def list_projects(self) -> list[Project]:
        """Return the caller's Project memberships in endpoint order.

        This request is authenticated but never scoped to a selected Project,
        so no ``X-Group`` header is sent.
        """
        page = _parse(ProjectPage, self._request("GET", "/projects", project_scoped=False))
        return list(page.items)

    def list_triggers(
        self, *, limit: int = 50, offset: int = 0, enabled: bool | None = None
    ) -> OffsetPage[Trigger]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if enabled is not None:
            params["enabled"] = enabled
        return _parse(OffsetPage[Trigger], self._request("GET", "/triggers", params=params))

    def get_trigger(self, trigger_id: UUID) -> Trigger:
        return _parse(Trigger, self._request("GET", f"/triggers/{trigger_id}"))

    def set_trigger_enabled(self, trigger_id: UUID, enabled: bool) -> Trigger:
        return _parse(Trigger, self._request("PATCH", f"/triggers/{trigger_id}", json={"enabled": enabled}))

    def list_events(
        self,
        trigger_id: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: str | None = None,
        source: str | None = None,
    ) -> CursorPage[Event]:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        if status is not None:
            params["status"] = status
        if source is not None:
            params["source"] = source
        return _parse(
            CursorPage[Event], self._request("GET", f"/triggers/{trigger_id}/events", params=params)
        )

    def s3_credentials(self, trigger_id: UUID, *, duration_seconds: int = 900) -> S3Credentials:
        body = self._request(
            "POST",
            f"/triggers/{trigger_id}/s3/credentials",
            json={"duration_seconds": duration_seconds},
            sensitive=True,
        )
        return _parse(S3Credentials, body, secret_response=True)

    def s3_upload_url(self, trigger_id: UUID, key: str, *, duration_seconds: int = 900) -> UploadURL:
        body = self._request(
            "POST",
            f"/triggers/{trigger_id}/s3/upload-url",
            json={"key": key, "duration_seconds": duration_seconds},
            sensitive=True,
        )
        return _parse(UploadURL, body, secret_response=True)


def _parse(model: Any, value: Any, *, secret_response: bool = False) -> Any:
    try:
        return TypeAdapter(model).validate_python(value)
    except ValidationError as exc:
        message = (
            "Server returned an unexpected secrets-bearing response. No values were displayed."
            if secret_response
            else "Server response did not match the expected EBS API format."
        )
        raise CLIError(message) from exc


def _response_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        messages = [str(item.get("msg")) for item in detail[:3] if isinstance(item, dict) and item.get("msg")]
        return "; ".join(messages)
    return ""
