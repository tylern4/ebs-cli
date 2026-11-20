from pathlib import Path

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError

from ebs_cli.errors import CLIError
from ebs_cli.models import S3Credentials
from ebs_cli.s3ops import s3_client


def upload_file(credentials: S3Credentials, path: Path, key: str) -> None:
    """Upload with the scoped temporary credentials and boto3's managed transfer."""
    try:
        s3_client(credentials).upload_file(str(path), credentials.bucket, key)
    except (BotoCoreError, ClientError, S3UploadFailedError, OSError) as exc:
        raise CLIError(
            "S3 upload failed. Check the file and network, "
            "or request new temporary credentials if these expired."
        ) from exc
