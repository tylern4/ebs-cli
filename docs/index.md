# ebs-cli guides

These guides cover NERSC Event Based Scheduling and its `ebs` command-line client. Start with the [quick start](../README.md) in the README, then use the guide you need:

| Guide | Contents |
| --- | --- |
| [FITS pipeline with the web interface](fits-starfinder-web-ui.md) | A complete FITS upload and mock `starfinder` batch-job walkthrough in the browser |
| [FITS pipeline with the CLI](fits-starfinder-cli.md) | The same mock star-finding workflow using YAML trigger documents and `ebs` commands |
| [Using the web interface](web-interface.md) | Signing in, Projects, tokens, triggers, events, and upload access in the browser |
| [Authentication and Projects](authentication.md) | Login options, token refresh and logout, and choosing a Project |
| [Triggers and events](triggers.md) | Listing, filtering, pagination, and trigger references |
| [S3 operations and boto3](s3.md) | Scoped temporary credentials, object commands, uploads, and presigned URLs |
| [Development and CI](development.md) | For contributors: tests, linting, builds, and continuous integration |

The web interface for the same service is at <https://ebs-poc.nersc.gov>; see [Using the web interface](web-interface.md) for a walkthrough of its workflows.
