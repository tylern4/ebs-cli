# Authentication and Projects

Return to the [guide index](index.md).

## Server and authentication

The default server is `https://ebs-poc.nersc.gov`. Set another origin with `--url` or `EBS_URL`:

```sh
export EBS_URL=http://localhost:8000
```

The value is an HTTP or HTTPS origin only. Do not include `/api/v1`, a query, or credentials.

Create a personal token in the [EBS web interface](https://ebs-poc.nersc.gov), then log in:

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

Next: [Triggers and events](triggers.md).
