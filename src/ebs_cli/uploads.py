from pathlib import Path

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ebs_cli.errors import CLIError
from ebs_cli.models import S3Credentials


def upload_file(credentials: S3Credentials, path: Path, key: str) -> None:
    """Upload with the scoped temporary credentials and boto3's managed transfer."""
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=10,
        read_timeout=120,
        retries={"max_attempts": 3, "mode": "standard"},
    )
    try:
        client = boto3.client(
            "s3",
            endpoint_url=credentials.endpoint_url,
            region_name=credentials.region,
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            aws_session_token=credentials.session_token,
            config=config,
        )
        client.upload_file(str(path), credentials.bucket, key)
    except (BotoCoreError, ClientError, S3UploadFailedError, OSError) as exc:
        raise CLIError(
            "S3 upload failed. Check the file and network, "
            "or request new temporary credentials if these expired."
        ) from exc
