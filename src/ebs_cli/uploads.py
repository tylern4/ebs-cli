from pathlib import Path

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError
from rich.progress import BarColumn, DownloadColumn, Progress, TaskProgressColumn, TextColumn

from ebs_cli.errors import CLIError
from ebs_cli.models import S3Credentials
from ebs_cli.output import error_console, safe_text
from ebs_cli.s3ops import s3_client


def upload_file(credentials: S3Credentials, path: Path, key: str) -> None:
    """Upload with the scoped temporary credentials and boto3's managed transfer."""
    interactive = error_console.is_terminal
    try:
        with Progress(
            TextColumn("Uploading {task.description}", markup=False),
            BarColumn(),
            TaskProgressColumn(),
            DownloadColumn(),
            console=error_console,
            disable=not interactive,
            redirect_stdout=False,
            redirect_stderr=False,
        ) as progress:
            task = progress.add_task(safe_text(path.name), total=path.stat().st_size)

            def report_progress(bytes_amount: int) -> None:
                # Managed multipart transfers are concurrent and rewind a part's
                # bytes with a negative delta when an in-flight request is retried.
                progress.update(task, advance=bytes_amount)

            options = {"Callback": report_progress} if interactive else {}
            s3_client(credentials).upload_file(str(path), credentials.bucket, key, **options)
    except (BotoCoreError, ClientError, S3UploadFailedError, OSError) as exc:
        raise CLIError(
            "S3 upload failed. Check the file and network, "
            "or request new temporary credentials if these expired."
        ) from exc
