# ebs-cli

`ebs` is a command-line client for NERSC Event Based Scheduling. It discovers your Projects, lists and controls triggers, shows events with their jobs, and runs direct S3 object commands against a trigger's bucket using temporary, operation-scoped access.

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

The config file stores JSON with the token and an optional saved default Project:

```json
{"token": "ebs_…", "project": "physics"}
```

Token-only files from earlier versions keep working unchanged. `ebs projects use` adds the `project` field without touching the token, and `ebs auth login` keeps a saved default Project. A config with only a `project` works when authentication comes from `EBS_TOKEN`.

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

This removes only the token; a saved default Project is kept, and the config file is deleted when no settings remain. This does not revoke the token in EBS. Revoke it in the web interface when it is no longer needed.

## Discover Projects and set a default

List the Projects your account can access. This command works before any Project is selected:

```sh
ebs projects list
ebs projects list --json
```

The table shows each Project name and your role in it. Save a default after checking the exact name against the list:

```sh
ebs projects use physics
```

The name is validated against the same endpoint before anything is saved. Unknown, empty, or control-character names fail and leave the config file unchanged. The saved default never contains or displays your token.

Show the effective Project and where it comes from. This reads only local configuration and never contacts the API:

```sh
ebs projects show
ebs projects show --json
```

The effective Project is chosen in this order:

1. The explicit `--project` option.
2. The `EBS_PROJECT` environment variable.
3. The saved default from `ebs projects use`.

After saving a default, plain commands work without flags or environment variables:

```sh
ebs triggers list
```

Without any of the three, API commands fail with guidance to run `ebs projects list` and `ebs projects use PROJECT`. You can still override the saved default for one call with `--project` or `EBS_PROJECT`. The global options precede the command group: `ebs --project physics --url http://localhost:8000 triggers list`.

## Triggers

List one page, filter by name, show a trigger, or change its state:

```sh
ebs triggers list --limit 50 --offset 0
ebs triggers list --enabled
ebs triggers list --disabled
ebs triggers list --name data
ebs triggers show data-ready
ebs triggers disable data-ready
ebs triggers enable 12345678-1234-5678-1234-567812345678
```

Pages have at most 100 triggers. Use the reported total and `--offset` to request another page. `--name` is a case-insensitive substring filter that stays within the selected Project and one bounded page.

### Trigger references

Every command that names a trigger — `triggers show`, `enable`, and `disable`, `events list`, and every `s3` command — accepts a `TRIGGER_REF`: either the trigger's ID (UUID) or its exact, case-sensitive name in the selected or default Project:

```sh
ebs triggers show data-ready
ebs events list data-ready
ebs s3 ls data-ready --prefix run/
```

A syntactically valid UUID is used directly, with no name lookup and unchanged behavior. Any other reference is resolved with one bounded, Project-scoped listing filtered by that name, and must match exactly: no partial or case-insensitive match is ever chosen silently. Zero matches fail with guidance to run `ebs triggers list --name REF`; a name shared by several triggers fails closed and lists their trigger IDs so you can pick one; when too many triggers resemble the name to inspect in one bounded page, use the trigger ID instead. Resolution never contacts another Project and never displays credentials or signed URLs. `--json` output is identical for both reference forms.

## Events and jobs

The trigger can be a UUID or an exact name (see [Trigger references](#trigger-references)):

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

Each exchange is scoped to one operation. Request exactly the scope you need with `--operation`; the server derives the least-privilege policy from it:

| Operation | Allows | Withholds |
| --- | --- | --- |
| `upload` (default) | Put objects, multipart uploads, and bucket listing | Reading or deleting objects |
| `list` | One bounded `ListObjectsV2` page | Reading, writing, deleting |
| `download` | Reading one specified object | Listing, writing, deleting |
| `delete` | Deleting one specified object | Listing, reading, writing |

The exchange never grants access to another Project's bucket, and the credentials expire when the requested lifetime ends.

The human-readable output is a Python example that passes all three temporary values directly to boto3. It sets the returned endpoint and region, SigV4 signing, path-style addressing, and connection timeouts, and the closing lines match the selected operation. For the default `upload` scope it uses boto3's managed `upload_file`, which can switch to multipart uploads for large files:

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

## List objects

Exchange for `list` credentials and fetch exactly one bounded page of objects:

```sh
ebs s3 ls 12345678-1234-5678-1234-567812345678
ebs s3 ls 12345678-1234-5678-1234-567812345678 --prefix run/ --limit 500 --json
```

The page size is 100 by default and `--limit` accepts 1 through 1000. The command never lists all buckets and never fetches unbounded pages. The table shows each object key, size in bytes, and last-modified time. When the store reports another page, the output shows the continuation token to pass to the next call as `--cursor TOKEN`; otherwise there is nothing more in this result set.

`--json` writes the bucket, effective prefix, page limit, and the objects as `key`, `size`, and `last_modified` (UTC ISO-8601). The `next_cursor` field appears only when the server returned a continuation token.

## Download an object

Exchange for `download` credentials, then stream one object to a local file:

```sh
ebs s3 download 12345678-1234-5678-1234-567812345678 run/data.fits ./data.fits
```

The object body is streamed to an owner-only temporary file in the destination directory and renamed into place only after the transfer completes, so a partial download never masquerades as a complete file. A destination that already exists is refused unless you pass `--force`, and a destination that is a symbolic link is always refused, even with `--force`, so nothing is written through the link.

Failures clean up the temporary file and print only that the download failed; temporary credentials and S3 exception details are never shown. Successful output contains only the `s3://bucket/key` source and the local destination.

## Delete one object

Exchange for `delete` credentials and remove exactly one object:

```sh
ebs s3 rm 12345678-1234-5678-1234-567812345678 run/data.fits
```

Attached to a terminal, the command asks for confirmation naming the key and trigger before any credential is requested. In scripts or other noninteractive sessions, deletion requires `--yes`; without it the command refuses and requests nothing. Prefixes and recursive trees are never deleted — only the exact key you name.

Successful output contains only the `s3://bucket/key` target and the deleted status.

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

The CLI requests temporary scoped credentials for the `upload` operation, then gives boto3's managed `upload_file` the local path, returned bucket, and derived object key. The client uses the returned endpoint and region, all three temporary values, SigV4, path-style addressing, and bounded connection timeouts. Managed upload supports multipart transfers without loading the whole file into memory.

The local file never goes to the EBS API, and the EBS bearer token never goes to S3. This command does not request or display a presigned URL. Successful output contains only the `s3://bucket/key` target and status. Temporary credentials stay out of both success and failure output.

## JSON output

Read commands and mutations support `--json` after the leaf command:

```sh
ebs triggers list --json
ebs events list 12345678-1234-5678-1234-567812345678 --json
ebs projects list --json
ebs projects show --json
ebs s3 credentials 12345678-1234-5678-1234-567812345678 --json
ebs s3 ls 12345678-1234-5678-1234-567812345678 --json
ebs s3 download 12345678-1234-5678-1234-567812345678 run/data.fits ./data.fits --json
ebs s3 rm 12345678-1234-5678-1234-567812345678 run/data.fits --yes --json
ebs s3 upload 12345678-1234-5678-1234-567812345678 ./data.fits --json
```

JSON goes to stdout with sorted keys. Diagnostics go to stderr and failures return a nonzero status. Credential JSON intentionally contains secrets. Object-command JSON contains only targets, paths, page data, and success status — never temporary credentials or a signed URL.

## Development

Run the full checks from the project root:

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ebs --help
uv run ebs projects list --help
uv run ebs projects use --help
uv run ebs projects show --help
uv run ebs triggers list --help
uv run ebs triggers show --help
uv run ebs triggers enable --help
uv run ebs triggers disable --help
uv run ebs events list --help
uv run ebs s3 credentials --help
uv run ebs s3 upload-url --help
uv run ebs s3 upload --help
uv run ebs s3 ls --help
uv run ebs s3 download --help
uv run ebs s3 rm --help
uv build
```

Tests use mocked HTTP transports and temporary config files. They do not contact a deployed server or upload live data.

## Continuous integration

GitHub Actions runs the same checks on every push to `main`, every pull request, and on manual dispatch. Each run uses Ubuntu with Python 3.11 and 3.14 and installs the locked dependencies with `uv sync --locked`, then:

- runs the full pytest suite,
- checks Ruff lint, formatting, and import ordering,
- smoke-tests `ebs --version` and `--help` for every command, and
- builds a wheel and a source distribution and verifies both contain every module in `src/ebs_cli`.

The tests are fully mocked, so CI needs no credentials and never contacts a deployed server or an object store. The workflow requests only read access to the repository and does not publish anything; the build artifacts it produces stay untracked, like `uv build` output locally.
