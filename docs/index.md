# ebs-cli guides

These guides cover the `ebs` command-line client for NERSC Event Based Scheduling in depth. Start with the [quick start](../README.md) in the README, then use the guide you need:

| Guide | Contents |
| --- | --- |
| [Using the web interface](web-interface.md) | Signing in, Projects, tokens, triggers, events, and upload access in the browser |
| [Authentication and Projects](authentication.md) | Globus browser login, manual tokens, refresh/logout, config security, and Project precedence |
| [Triggers and events](triggers.md) | Listing, filtering, pagination, and trigger references |
| [S3 operations and boto3](s3.md) | Scoped temporary credentials, object commands, uploads, and presigned URLs |
| [Development and CI](development.md) | Tests, linting, builds, and continuous integration |

The web interface for the same service is at <https://ebs-poc.nersc.gov>; see [Using the web interface](web-interface.md) for a walkthrough of its workflows.
