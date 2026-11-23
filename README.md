# ebs-cli

`ebs` is a command-line client for NERSC Event Based Scheduling. It discovers your Projects, lists and controls triggers, shows events with their jobs, and runs direct S3 object commands against a trigger's bucket using temporary, operation-scoped access.

**Try the web interface at <https://ebs-poc.nersc.gov>**. It shows the same Projects, triggers, events, and jobs, and it is where you create your personal API token.

![Overview of Projects, triggers, events, and jobs in the EBS web interface](docs/images/ebs-overview.png)

![A trigger with its recent events and jobs in the EBS web interface](docs/images/ebs-trigger.png)

## Quick start

Python 3.11 or newer is required. `uv` installs a managed Python if the current one is too old.

```sh
git clone https://github.com/tylern4/ebs-cli.git
cd ebs-cli
uv sync
source .venv/bin/activate
```

Log in through Globus in your browser:

```sh
ebs auth login
```

The CLI uses a Native App authorization-code + PKCE flow, then saves the
personal EBS token, Globus refresh credential, and public client ID together in
an owner-only config file. No web-client secret or short-lived access token is
saved. Personal tokens expire after 48 hours. If an ordinary API request then
receives HTTP 401, the saved Globus grant replaces the token and retries once;
this never applies to `EBS_TOKEN`, manual token login, or direct S3 operations.
Use `ebs auth refresh` for an explicit earlier rotation, and `ebs auth logout`
to clear both saved login credentials (plus revocation of the CLI grant when
Globus confirms it). Over SSH, run `ebs auth login --no-browser`,
open the displayed URL on any device, and paste the Globus one-time code into
the hidden prompt. This mode opens no local callback listener.

For manual token bootstrap or automation, `ebs auth login --token` selects a
hidden prompt for a personal token from the web interface. `--token` is only a
switch and never accepts a value, keeping credentials out of command arguments
and shell history. `EBS_TOKEN` remains available for ephemeral environments.

List your Projects and save a default:

```sh
ebs projects list
ebs projects use PROJECT
```

Use the exact Project name from the list. After this, plain commands work without flags or environment variables.

The Project listing also shows a `Triggers` count per Project (`trigger_count` with `--json`), so you can see which Projects have triggers configured before listing them individually:

```sh
$ ebs projects list
Projects
┏━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ Project ┃ Role        ┃ Triggers ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ alpha   │ maintainer  │        2 │
│ zeta    │ user        │        0 │
└─────────┴─────────────┴──────────┘
```

A few first commands:

```sh
ebs triggers list
ebs triggers show TRIGGER
ebs events list TRIGGER
ebs s3 ls TRIGGER --prefix run/
```

`TRIGGER` is a trigger's ID (UUID) or its exact name in the selected Project. To upload a local file straight to a trigger's bucket:

```sh
ebs s3 upload TRIGGER ./data.fits
```

## Guides

| Guide | Contents |
| --- | --- |
| [FITS pipeline with the web interface](docs/fits-starfinder-web-ui.md) | Upload FITS files and process them with a mock `starfinder` batch-job trigger |
| [FITS pipeline with the CLI](docs/fits-starfinder-cli.md) | The same mock star-finding workflow with YAML trigger documents and `ebs` commands |
| [Web interface](docs/web-interface.md) | Signing in, Projects, tokens, triggers, events, and upload access in the browser |
| [Authentication and Projects](docs/authentication.md) | Globus browser login, manual tokens, refresh/logout, config security, and Project precedence |
| [Triggers and events](docs/triggers.md) | Listing, filtering, pagination, trigger references |
| [S3 operations and boto3](docs/s3.md) | Scoped temporary credentials, object commands, uploads, presigned URLs |
| [Development and CI](docs/development.md) | Tests, linting, builds, and continuous integration |

The CLI uses personal EBS API tokens. It never asks for the object store's root key or system credentials, and it keeps your EBS token and S3 credentials out of each other's requests. One exception is deliberate: `ebs s3 credentials` prints the temporary access key, secret key, and session token to stdout, so protect that output and keep it out of logs and shared terminals. The direct S3 commands (`s3 ls`, `s3 download`, `s3 rm`, `s3 upload`) do not display temporary credentials, and signed URLs stay redacted unless you pass `--show-url`.
