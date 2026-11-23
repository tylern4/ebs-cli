# Authentication and Projects

Return to the [guide index](index.md).

The CLI connects to [NERSC Event Based Scheduling](https://ebs-poc.nersc.gov).

## Log in with your browser

```sh
ebs auth login
```

This opens your system web browser. Sign in with your NERSC identity through Globus, approve the request, and the CLI finishes on its own. Your personal token is saved locally, and you are ready to run commands.

## Log in over SSH

On a machine without a browser, use:

```sh
ebs auth login --no-browser
```

The CLI prints a URL. Open it in a browser on any device (your laptop or phone), sign in, and Globus shows a one-time code. Paste that code into the hidden prompt in the terminal. No port forwarding is needed.

## Log in with a personal token

If you prefer to create a token yourself, make one in the [EBS web interface](https://ebs-poc.nersc.gov) (see [Using the web interface](web-interface.md#create-a-personal-token-for-the-cli)), then run:

```sh
ebs auth login --token
```

and paste the token at the hidden prompt.

`--token` is only a switch: it never takes a token as a value. Never put a token in a command argument, script, or shell history.

For a short-lived shell or automation, an environment variable works too:

```sh
read -rsp "Personal EBS API token: " EBS_TOKEN
export EBS_TOKEN
echo
```

Tokens created by the CLI login are labeled `ebs-cli-native-login` in the web interface, where you can revoke old ones you no longer need.

## Where your credentials live

The CLI stores your saved token and login details in `~/.config/ebs/config.json`, readable only by you. `XDG_CONFIG_HOME` changes the base directory, and `EBS_CONFIG` selects an exact file path.

- If the CLI warns that the file is readable by others, fix it with `chmod 600 ~/.config/ebs/config.json`.
- Keep the file out of backups you share and out of any repository.
- Personal tokens and temporary S3 credentials are secrets: keep them out of logs, screenshots, and command arguments. `ebs s3 credentials` deliberately prints temporary keys to stdout — protect that output.

## When tokens expire

Personal tokens expire 48 hours after they are created.

- **Saved browser login:** your token renews automatically. When it expires, the next CLI command renews it and retries — you normally do nothing.
- Run `ebs auth refresh` any time you want to rotate the token early.
- **Not renewed automatically:** a token set with `EBS_TOKEN` or pasted at the `--token` prompt, and direct S3 commands. If an S3 command reports an expired or invalid token, run `ebs auth refresh` (or log in again) and retry.

To log out and revoke the CLI's saved login:

```sh
ebs auth logout
```

This clears the local credentials and asks Globus to revoke the CLI's grant. If revocation cannot be confirmed (for example, offline), the CLI says so — revoke the grant from your Globus account later. To skip the revocation request entirely, use `ebs auth logout --local-only`.

Logging out does not revoke other browser sessions or personal tokens you created in the web interface; revoke those on the Settings page.

## Discover Projects and set a default

Most commands operate inside one of your Projects. List the Projects your account can access — this works before any Project is selected:

```sh
ebs projects list
ebs projects list --json
```

The table shows each Project's name, your role in it, and how many triggers it has. Save a default using the exact name from the list:

```sh
ebs projects use physics
```

Check where the default comes from at any time (reads only local configuration, no API call):

```sh
ebs projects show
ebs projects show --json
```

The effective Project is chosen in this order:

1. The `--project` (or `-p`) option.
2. The `EBS_PROJECT` environment variable.
3. The saved default from `ebs projects use`.

After saving a default, plain commands need no flags or environment variables:

```sh
ebs triggers list
```

You can still override the default for a single call; global options come before the command group:

```sh
ebs --project physics triggers list
```

Without any Project selected, API commands fail with guidance to run `ebs projects list` and `ebs projects use PROJECT`.

Next: [Triggers and events](triggers.md).
