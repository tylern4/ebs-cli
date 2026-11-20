"""Direct boto3 object operations backed by operation-scoped temporary credentials.

Every operation builds its client only from the values the EBS server returned
for one trigger's bucket: the endpoint, region, all three temporary values, and
the returned bucket itself. Callers cannot substitute another bucket, and no
temporary value is ever included in an error message.
"""

import os
import stat
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ebs_cli.errors import CLIError
from ebs_cli.models import S3Credentials

DOWNLOAD_CHUNK_BYTES = 64 * 1024


def s3_client(credentials: S3Credentials) -> Any:
    """Build a boto3 client with SigV4, path-style addressing, timeouts, and retries.

    Callers wrap construction in their own error handling, so a failure here
    surfaces as that operation's safe CLIError instead of a second message.
    """
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=10,
        read_timeout=120,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    return boto3.client(
        "s3",
        endpoint_url=credentials.endpoint_url,
        region_name=credentials.region,
        aws_access_key_id=credentials.access_key_id,
        aws_secret_access_key=credentials.secret_access_key,
        aws_session_token=credentials.session_token,
        config=config,
    )


def list_objects(
    credentials: S3Credentials, *, prefix: str, limit: int, cursor: str | None
) -> dict[str, Any]:
    """Return one bounded page of objects plus the continuation token, if any."""
    request: dict[str, Any] = {"Bucket": credentials.bucket, "MaxKeys": limit}
    if prefix:
        request["Prefix"] = prefix
    if cursor:
        request["ContinuationToken"] = cursor
    try:
        response = s3_client(credentials).list_objects_v2(**request)
    except (BotoCoreError, ClientError, OSError) as exc:
        raise CLIError(
            "S3 listing failed. Check the prefix and cursor, "
            "or request new temporary credentials if these expired."
        ) from exc
    contents = response.get("Contents") if isinstance(response, dict) else None
    items = []
    for entry in contents or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("Key"), str):
            continue
        size = entry.get("Size")
        items.append(
            {
                "key": entry["Key"],
                "size": size if isinstance(size, int) and size >= 0 else 0,
                "last_modified": _iso_time(entry.get("LastModified")),
            }
        )
    token = response.get("NextContinuationToken") if isinstance(response, dict) else None
    return {"items": items, "next_cursor": token if isinstance(token, str) and token else None}


def download_object(credentials: S3Credentials, key: str, destination: Path, *, force: bool = False) -> None:
    """Stream one object to an owner-only temporary file, then atomically rename it.

    The temporary file lives in the destination directory, is never readable by
    other users, and is removed when the transfer fails. Exception details and
    credentials never reach the caller.
    """
    directory = destination.parent
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=directory
        )
    except OSError as exc:
        raise CLIError(f"Could not create a temporary file inside {directory}.") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            response = s3_client(credentials).get_object(Bucket=credentials.bucket, Key=key)
            body = response["Body"] if isinstance(response, dict) else None
            if body is None:
                raise TypeError("S3 returned no object body")
            for chunk in body.iter_chunks(chunk_size=DOWNLOAD_CHUNK_BYTES):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            destination_stat = destination.lstat()
        except FileNotFoundError:
            destination_stat = None
        # Repeat the destination checks after the transfer, immediately before
        # the atomic rename. Never follow or replace a symlink that appeared
        # while downloading; an existing file requires explicit --force.
        if destination_stat is not None and (stat.S_ISLNK(destination_stat.st_mode) or not force):
            raise FileExistsError("download destination appeared during transfer")
        os.replace(temporary, destination)
    except (BotoCoreError, ClientError, OSError, TypeError, KeyError) as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise CLIError(
            "S3 download failed. Check the object key and network, "
            "or request new temporary credentials if these expired."
        ) from exc


def delete_object(credentials: S3Credentials, key: str) -> None:
    """Delete exactly one object; never a prefix or a recursive tree."""
    try:
        s3_client(credentials).delete_object(Bucket=credentials.bucket, Key=key)
    except (BotoCoreError, ClientError, OSError) as exc:
        raise CLIError(
            "S3 delete failed. The object may still exist; request new temporary credentials and retry."
        ) from exc


def _iso_time(value: Any) -> str:
    """Render an S3 timestamp as a stable UTC ISO-8601 string."""
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
