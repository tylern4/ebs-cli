# ebs-cli

`ebs` is a command-line client for NERSC Event Based Scheduling. It lists and controls triggers, shows events with their jobs, and uploads data to a trigger's S3 bucket using temporary, scoped access.

The CLI uses personal EBS API tokens. It never asks for the object store's root key or system credentials.

## Install with uv

Python 3.11 or newer is required. `uv` installs a managed Python if the current one is too old.

```sh
git clone https://github.com/tylern4/ebs-cli.git
cd ebs-cli
uv sync
```

The virtual environment contains `ebs`, its dependencies, and the test tools. Run commands as `uv run ebs`, or activate the environment first:

```sh
source .venv/bin/activate
ebs --help
```

To build an installable wheel and source archive locally, run `uv build`. Nothing is published by that command.

## Server and authentication

The default server is `https://ebs-poc.nersc.gov`. Set another origin with `--url` or `EBS_URL`:

```sh
export EBS_URL=http://localhost:8000
```

The value is an HTTP or HTTPS origin only. Do not include `/api/v1`, a query, or credentials.

Create a personal token in the EBS web interface, then log in:

```sh
ebs auth login
```

The prompt hides the token. There is deliberately no `--token` option, so tokens do not end up in shell history or command-line completion. By default, the CLI writes `~/.config/ebs/config.json` with mode `0600`, readable and writable only by its owner. `XDG_CONFIG_HOME` changes the base directory, and `EBS_CONFIG` selects an exact config path for isolated environments. The CLI rejects a saved config owned by another user or accessible by group/other users. Fix its permissions with `chmod 600 PATH`.

For an ephemeral shell or automation, prefer an environment variable:

```sh
read -rsp "Personal EBS API token: " EBS_TOKEN
export EBS_TOKEN
echo
```

`EBS_TOKEN` takes precedence over the config file. Do not put tokens in scripts, repository files, command arguments, or CI logs.

Log out by removing the saved token:

```sh
ebs auth logout
```

This does not revoke the token in EBS. Revoke it in the web interface when it is no longer needed.

## Select a Project

API commands require an explicit Project selection. The CLI sends that selection with every request and leaves access checks to EBS.

```sh
ebs --project physics triggers list
```

For repeated commands, set `EBS_PROJECT`:

```sh
export EBS_PROJECT=physics
ebs triggers list
```

The global options precede the command group: `ebs --project physics --url http://localhost:8000 triggers list`.

## Triggers

List one page, show a trigger, or change its state:

```sh
ebs triggers list --limit 50 --offset 0
ebs triggers list --enabled
ebs triggers list --disabled
ebs triggers show 12345678-1234-5678-1234-567812345678
ebs triggers disable 12345678-1234-5678-1234-567812345678
ebs triggers enable 12345678-1234-5678-1234-567812345678
```

Pages have at most 100 triggers. Use the reported total and `--offset` to request another page.

## Events and jobs

```sh
ebs events list 12345678-1234-5678-1234-567812345678
ebs events list 12345678-1234-5678-1234-567812345678 \
  --status failed --source s3 --limit 50
```

Each call fetches one bounded page of at most 200 events. When the response has another page, use its cursor exactly as returned:

```sh
ebs events list 12345678-1234-5678-1234-567812345678 \
  --cursor OPAQUE_CURSOR_FROM_PREVIOUS_RESPONSE
```

The table includes the associated job ID, job status, and a bounded failure reason. Event payloads and rendered job scripts are not printed. Failure text is treated as plain text, not terminal markup.

## Temporary credentials for boto3

An S3 trigger can issue temporary credentials restricted to its own bucket:

```sh
ebs s3 credentials 12345678-1234-5678-1234-567812345678
```

This command intentionally prints the access key, secret key, and session token to stdout. Keep them out of logs and shared terminals. The default lifetime is 900 seconds. Choose 900 through 3600 seconds with `--duration-seconds`.

The human-readable output is a Python example that passes all three temporary values directly to boto3. It sets the returned endpoint and region, SigV4 signing, path-style addressing, and connection timeouts. It uses boto3's managed `upload_file`, which can switch to multipart uploads for large files:

```python
import boto3
from botocore.config import Config

s3 = boto3.client(
    "s3",
    endpoint_url="https://s3.example.test",
    region_name="us-east-1",
    aws_access_key_id="TEMPORARY_ACCESS_KEY",
    aws_secret_access_key="TEMPORARY_SECRET_KEY",
    aws_session_token="TEMPORARY_SESSION_TOKEN",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=10,
        read_timeout=120,
    ),
)
s3.upload_file("./data.fits", "TRIGGER_BUCKET", "incoming/data.fits")
```

Replace the placeholders with the returned values and adjust the local path and object key. The credentials expire and do not grant access to another Project's bucket.

For shell environment exports instead of Python instructions:

```sh
ebs s3 credentials 12345678-1234-5678-1234-567812345678 --shell
```

```sh
export AWS_ACCESS_KEY_ID='TEMPORARY_ACCESS_KEY'
export AWS_SECRET_ACCESS_KEY='TEMPORARY_SECRET_KEY'
export AWS_SESSION_TOKEN='TEMPORARY_SESSION_TOKEN'
export AWS_DEFAULT_REGION='us-east-1'
export EBS_S3_ENDPOINT='https://s3.example.test'
```

For automation, `--json` returns the API fields as JSON. Direct evaluation with `eval` is not recommended; shell output is intended for inspection or a carefully controlled shell.

## One-object upload URLs

Generate a presigned PUT URL for one object key:

```sh
ebs s3 upload-url 12345678-1234-5678-1234-567812345678 incoming/data.fits
```

The output shows the `s3://` target, expiry, and a `curl --request PUT --upload-file` command. The signed URL is replaced with `[signed URL redacted]` by default because anyone holding it can upload that object until it expires.

When you intentionally need to run or copy the complete command, reveal it:

```sh
ebs s3 upload-url 12345678-1234-5678-1234-567812345678 incoming/data.fits --show-url
```

Do not send the revealed URL through chat, logs, tickets, or untrusted command history. `--json` also redacts the URL unless `--show-url` is present.

## Upload a local file

Upload directly to the trigger's bucket without copying the file through EBS:

```sh
ebs s3 upload 12345678-1234-5678-1234-567812345678 ./data.fits
ebs s3 upload 12345678-1234-5678-1234-567812345678 ./data.fits --key incoming/run-42.fits
```

The default object key is the local file name. Keys must be relative, at most 1024 UTF-8 bytes, and cannot contain empty, `.` or `..` path segments.

The CLI requests temporary scoped credentials, then gives boto3's managed `upload_file` the local path, returned bucket, and derived object key. The client uses the returned endpoint and region, all three temporary values, SigV4, path-style addressing, and bounded connection timeouts. Managed upload supports multipart transfers without loading the whole file into memory.

The local file never goes to the EBS API, and the EBS bearer token never goes to S3. This command does not request or display a presigned URL. Successful output contains only the `s3://bucket/key` target and status. Temporary credentials stay out of both success and failure output.

## JSON output

Read commands and mutations support `--json` after the leaf command:

```sh
ebs triggers list --json
ebs events list 12345678-1234-5678-1234-567812345678 --json
ebs s3 credentials 12345678-1234-5678-1234-567812345678 --json
ebs s3 upload 12345678-1234-5678-1234-567812345678 ./data.fits --json
```

JSON goes to stdout with sorted keys. Diagnostics go to stderr and failures return a nonzero status. Credential JSON intentionally contains secrets. Upload JSON contains the target and success status, never temporary credentials or a signed URL.

## Development

Run the full checks from the project root:

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ebs --help
uv run ebs triggers list --help
uv run ebs events list --help
uv run ebs s3 credentials --help
uv run ebs s3 upload-url --help
uv run ebs s3 upload --help
uv build
```

Tests use mocked HTTP transports and temporary config files. They do not contact a deployed server or upload live data.
