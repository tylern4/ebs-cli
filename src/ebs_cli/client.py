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

MAX_TRIGGER_NAME_BYTES = 255
NAME_LOOKUP_LIMIT = 100
AMBIGUITY_SAMPLE_LIMIT = 5


def validate_trigger_name(value: str) -> str:
    """Validate a trigger name used as a listing filter or reference."""
    if not value or len(value.encode("utf-8")) > MAX_TRIGGER_NAME_BYTES:
        raise CLIError("Trigger name must be 1 to 255 UTF-8 bytes.", exit_code=2)
    if any(not character.isprintable() for character in value):
        raise CLIError("Trigger name cannot contain control characters.", exit_code=2)
    return value


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


def validate_prefix(value: str) -> str:
    if len(value.encode("utf-8")) > 1024 or any(not character.isprintable() for character in value):
        raise CLIError("Prefix must be at most 1024 printable UTF-8 bytes.", exit_code=2)
    return value


def validate_cursor(value: str) -> str:
    value = value.strip()
    if not value or len(value.encode("utf-8")) > 1024:
        raise CLIError("Cursor must be the opaque continuation token from an earlier page.", exit_code=2)
    if any(not character.isprintable() for character in value):
        raise CLIError("Cursor cannot contain control characters.", exit_code=2)
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
        headers: dict[str, str] | None = None,
        sensitive: bool = False,
        project_scoped: bool = True,
        allow_empty: bool = False,
    ) -> Any:
        try:
            response = self._http.request(
                method, f"{API_PREFIX}{path}", params=params, json=json, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise CLIError("Request timed out. Check the server URL and retry.") from exc
        except httpx.RequestError as exc:
            raise CLIError(
                "Could not reach the EBS server. Check the server URL and network connection."
            ) from exc

        if response.is_success:
            if allow_empty and not response.content:
                return None
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

    def list_projects(self, *, include_trigger_counts: bool = False) -> list[Project]:
        """Return memberships, optionally with one-shot exact trigger totals.

        This request is authenticated but never scoped to a selected Project,
        so no ``X-Group`` header is sent. An older API omits ``trigger_count``
        from each item; callers can retain their bounded per-Project fallback.
        """
        params = {"include_trigger_counts": "true"} if include_trigger_counts else None
        page = _parse(
            ProjectPage,
            self._request("GET", "/projects", params=params, project_scoped=False),
        )
        return list(page.items)

    def list_triggers(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        enabled: bool | None = None,
        name: str | None = None,
    ) -> OffsetPage[Trigger]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if enabled is not None:
            params["enabled"] = enabled
        if name is not None:
            params["name"] = name
        return _parse(OffsetPage[Trigger], self._request("GET", "/triggers", params=params))

    def count_triggers(self, project: str) -> int:
        """Count every trigger in one explicitly named Project.

        The count is the ``total`` of a single bounded ``limit=1, offset=0``
        trigger listing, so only the total is retained rather than fetching
        the Project's full trigger list. The Project
        scope is sent as this request's ``X-Group`` header, which overrides
        any client-level selection, so a saved or flag-selected Project is
        never reused to count a different Project's triggers. Callers count
        each listed Project by its exact name.
        """
        page = _parse(
            OffsetPage[Trigger],
            self._request("GET", "/triggers", params={"limit": 1, "offset": 0}, headers={"X-Group": project}),
        )
        return page.total

    def get_trigger(self, trigger_id: UUID) -> Trigger:
        return _parse(Trigger, self._request("GET", f"/triggers/{trigger_id}"))

    def set_trigger_enabled(self, trigger_id: UUID, enabled: bool) -> Trigger:
        return _parse(Trigger, self._request("PATCH", f"/triggers/{trigger_id}", json={"enabled": enabled}))

    def delete_trigger(self, trigger_id: UUID) -> None:
        """Delete one trigger. The server owns cascade behavior and authorization."""
        self._request("DELETE", f"/triggers/{trigger_id}", allow_empty=True)

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

    def s3_credentials(
        self, trigger_id: UUID, *, duration_seconds: int = 900, operation: str | None = None
    ) -> S3Credentials:
        body: dict[str, Any] = {"duration_seconds": duration_seconds}
        if operation is not None:
            body["operation"] = operation
        response = self._request(
            "POST",
            f"/triggers/{trigger_id}/s3/credentials",
            json=body,
            sensitive=True,
        )
        return _parse(S3Credentials, response, secret_response=True)

    def s3_upload_url(self, trigger_id: UUID, key: str, *, duration_seconds: int = 900) -> UploadURL:
        body = self._request(
            "POST",
            f"/triggers/{trigger_id}/s3/upload-url",
            json={"key": key, "duration_seconds": duration_seconds},
            sensitive=True,
        )
        return _parse(UploadURL, body, secret_response=True)


def resolve_trigger_reference(client: EBSClient, reference: str) -> UUID:
    """Resolve a typed trigger reference to one trigger ID in the client's Project.

    A syntactically valid UUID is returned without any name lookup so the
    direct ``GET /triggers/{uuid}`` flow is unchanged. Any other nonempty
    reference is fetched with one bounded, name-filtered listing and matched
    exactly and case-sensitively against the returned trigger names. A match
    is accepted only when the whole candidate set fits in that bounded page,
    so a duplicate beyond the page can never be silently ignored. Zero,
    multiple, or truncated candidate sets fail closed without a request to
    any other Project.
    """
    try:
        return UUID(reference)
    except ValueError:
        pass
    name = validate_trigger_name(reference)
    page = client.list_triggers(name=name, limit=NAME_LOOKUP_LIMIT, offset=0)
    matches = [trigger for trigger in page.items if trigger.name == name]
    if len(matches) > 1:
        sample = ", ".join(str(trigger.id) for trigger in matches[:AMBIGUITY_SAMPLE_LIMIT])
        extra = len(matches) - AMBIGUITY_SAMPLE_LIMIT
        suffix = f" and {extra} more" if extra > 0 else ""
        raise CLIError(
            f"Trigger name '{name}' is ambiguous in the selected Project: "
            f"{len(matches)} triggers share it ({sample}{suffix}). Use a trigger ID instead.",
            exit_code=2,
        )
    if page.total > len(page.items):
        raise CLIError(
            f"The name '{name}' resembles {page.total} triggers in the selected Project, "
            "more than one bounded page can inspect. Use a trigger ID instead.",
            exit_code=2,
        )
    if len(matches) == 1:
        return matches[0].id
    raise CLIError(
        f"No trigger named '{name}' exists in the selected Project. "
        f"Run 'ebs triggers list --name {name}' to see similar names.",
        exit_code=2,
    )


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
        # FastAPI's list detail arrives in two shapes: the pipeline render
        # gate's plain sentences (list[str]) and pydantic validation
        # objects ({loc, msg, type}). Strings render verbatim; validation
        # objects render their msg. (issues/192, API-09: the render gate's
        # reason used to be blanked entirely here.)
        messages = []
        for item in detail[:3]:
            if isinstance(item, str):
                messages.append(item)
            elif isinstance(item, dict) and item.get("msg"):
                messages.append(str(item["msg"]))
        return "; ".join(messages)
    return ""
