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

Log in with a personal EBS API token from the web interface:

```sh
ebs auth login
```

The prompt hides the token, and it is saved to an owner-only config file. There is deliberately no `--token` option, so tokens do not end up in shell history or command-line completion.

List your Projects and save a default:

```sh
ebs projects list
ebs projects use PROJECT
```

Use the exact Project name from the list. After this, plain commands work without flags or environment variables.

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
| [Web interface](docs/web-interface.md) | Signing in, Projects, tokens, triggers, events, and upload access in the browser |
| [Authentication and Projects](docs/authentication.md) | Tokens, the config file, environment variables, Project selection and precedence |
| [Triggers and events](docs/triggers.md) | Listing, filtering, pagination, trigger references |
| [S3 operations and boto3](docs/s3.md) | Scoped temporary credentials, object commands, uploads, presigned URLs |
| [Development and CI](docs/development.md) | Tests, linting, builds, and continuous integration |

The CLI uses personal EBS API tokens. It never asks for the object store's root key or system credentials, and it keeps your EBS token and S3 credentials out of each other's requests. One exception is deliberate: `ebs s3 credentials` prints the temporary access key, secret key, and session token to stdout, so protect that output and keep it out of logs and shared terminals. The direct S3 commands (`s3 ls`, `s3 download`, `s3 rm`, `s3 upload`) do not display temporary credentials, and signed URLs stay redacted unless you pass `--show-url`.
