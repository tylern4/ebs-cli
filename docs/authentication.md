# Authentication and Projects

Return to the [guide index](index.md).

## Server and authentication

The default server is `https://ebs-poc.nersc.gov`. Set another origin with `--url` or `EBS_URL`:

```sh
export EBS_URL=http://localhost:8000
```

The value is an HTTP or HTTPS origin only. Do not include `/api/v1`, a query, or credentials.

## Browser login

Log in with the system's web browser:

```sh
ebs auth login
```

Browser login is a separate Globus Native App authorization-code flow with a
fresh random state and S256 PKCE verifier for every attempt. The CLI opens a
receiver only at `http://127.0.0.1:53682/oauth/callback`, waits at most three
minutes for one matching callback, exchanges the code directly with Globus,
and uses the short-lived access token once at `POST /api/v1/auth/cli/token` to
create a personal EBS token. It requests `openid profile email offline_access`;
this dedicated route verifies the Globus bearer and performs the same NERSC
identity/LDAP/core account synchronization as the web callback before using
normal personal-token creation. Ordinary `POST /api/v1/me/tokens` does not
perform login synchronization and is not the native CLI minting endpoint. There
is no web-client secret in the CLI and the default local flow does not ask for
an authorization code.

For an SSH session or other machine without a browser, use the manual code
flow:

```sh
ebs auth login --no-browser
```

Open the displayed URL in a browser on any device. After login and consent,
Globus displays a one-time authorization code. Copy only that code into the
CLI's hidden prompt. The CLI does not open a port or callback listener in this
mode, so no SSH port forward is needed. It uses Globus's native-app code-display
redirect, preserves fresh state and S256 PKCE, and sends the same code-display
redirect during token exchange. Do not paste a callback URL, token, or other
credential.

Only the default local flow uses the fixed loopback port, which cannot be shared
by another login or service. Denial, malformed callbacks, state mismatches,
callback timeout, and unavailable Native App configuration fail without saving
a login. Manual cancellation, empty or malformed codes, and failed exchanges
also fail without saving. Browser and refresh exchanges require HTTPS
EBS/provider endpoints; real flows accept only the canonical `auth.globus.org`
authorization/token endpoints (apart from explicitly injected test transports).
The HTTP local-server example above is for manual-token development only.

Only after both Globus and EBS token creation succeed does the CLI save the new
personal token, Globus refresh credential, and public Native App client ID in a
single owner-only atomic replacement. All config writers serialize their full
read-modify-write transactions through a persistent owner-only sibling lock;
logout never removes that lock. The CLI never saves the code, state, PKCE
verifier, or short-lived Globus access token. The file preserves a saved
Project and unrelated settings. A failed exchange or write does not claim
success or print either secret.

By default, the config is `~/.config/ebs/config.json` with mode `0600`.
`XDG_CONFIG_HOME` changes the base directory, and `EBS_CONFIG` selects an exact
path. The file must be a non-symlink regular file owned by the current user and
inaccessible to group/other users. Fix its permissions with `chmod 600 PATH`.
A browser-login config has this shape (values here are placeholders):

```json
{"token":"ebs_…","project":"physics","globus_refresh_token":"…","globus_client_id":"…"}
```

Token-only files from earlier versions keep working for API requests and manual
login. `ebs projects use` and both login/refresh updates retain the saved Project.

## Manual token bootstrap and automation

To use a personal token created in the [EBS web interface](https://ebs-poc.nersc.gov), select the hidden manual prompt explicitly:

```sh
ebs auth login --token
```

`--token` is only a switch. It never takes a token value; do not put a token in
a command argument, script, repository file, completion, or shell history. The
prompt hides its input. This path updates the personal EBS token while keeping
unrelated settings and an existing saved Project. It removes any older Globus
grant so a standalone manually supplied token never renews through that grant.

For an ephemeral shell or automation, prefer an environment variable:

```sh
read -rsp "Personal EBS API token: " EBS_TOKEN
export EBS_TOKEN
echo
```

`EBS_TOKEN` keeps its existing precedence over the config file for ordinary API
requests. `EBS_PROJECT`, `--project`, and saved Project selection are unchanged.
Environment and standalone manually supplied tokens never borrow a saved
Globus grant for renewal.

## Automatic renewal and explicit refresh

Personal EBS tokens have a fixed lifetime of 48 hours after creation. There is
no background timer or scheduled refresh. When an ordinary JSON API request
using the saved file's token receives HTTP 401, the CLI checks for a complete
Globus grant from the same saved snapshot. It exchanges that grant once,
atomically saves the replacement EBS token and any rotated Globus grant with
snapshot-conflict protection, updates the client's bearer header, and retries
the original request exactly once. A second 401 fails without another renewal.

Only authentication rejection triggers this behavior. HTTP 403, 404, redirects,
5xx responses, and network failures do not. Renewal uses neither `EBS_TOKEN` nor
a manually supplied token, and a changed saved file fails the conflict check
instead of spending or overwriting credentials. The short-lived Globus access
token remains in memory and is never saved. Bounded JSON API requests can be
replayed once; direct S3 operations, streaming bodies, and redirects cannot.

Use an existing saved Globus grant to rotate the personal EBS token explicitly:

```sh
ebs auth refresh
```

This exchanges the refresh credential directly with Globus, creates the new EBS
token, and atomically saves any rotated refresh credential. If Globus omits a
new refresh credential, the existing one is retained. A rejected or expired
grant leaves the saved file unchanged for diagnosis and directs you back to
`ebs auth login`; it does not fall back to printing credentials. Each login or
refresh creates a personal token with the stable `ebs-cli-native-login` label;
old personal tokens are managed separately in the web interface.

Log out of the local CLI and ask Globus to revoke only this CLI's saved refresh
grant:

```sh
ebs auth logout
```

Local EBS/Globus credentials are cleared together while the Project and other
settings remain; the file is deleted if nothing remains. Successful provider
revocation is reported only after Globus confirms it. If revocation is
unreachable or rejected, the local file is still cleared and the CLI says that
revocation was **not confirmed**; the remote grant may remain active. Revoke
that grant from your Globus account when connectivity returns. To guarantee an
offline/local-only operation with no revocation request, use:

```sh
ebs auth logout --local-only
```

That mode explicitly leaves the provider grant unrevoked. Neither logout mode
revokes other browser/web sessions or the personal EBS token on the server;
revoke unneeded personal tokens in the EBS web interface.

## Administrator Native App registration

Register one **separate public Globus Native App**, with authorization-code and
refresh-token grants and the `openid profile email offline_access` scopes. Do
not create or configure a client secret. Register both exact redirect URIs:

```text
http://127.0.0.1:53682/oauth/callback
https://auth.globus.org/v2/web/auth-code
```

The first supports the default local callback and the second is Globus's
official native-app code-display page for `--no-browser`. For loopback, the
scheme, numeric IPv4 loopback host, port, and path are all significant; do not
substitute `localhost` or an ephemeral port. Do not add other redirects.
Configure only the resulting
public client ID as user-api's `EBS_GLOBUS_CLI_CLIENT_ID` (Spin values key
`auth.globusCliClientId`). Empty configuration makes
`GET /api/v1/auth/cli-config` unavailable rather than exposing another client's
configuration. The endpoint returns only the public client ID and configured
provider authorization/token URLs. No actual registration or live provider/EBS
login verification is implied by the local test suite.

## Discover Projects and set a default

List the Projects your account can access. This command works before any Project is selected:

```sh
ebs projects list
ebs projects list --json
```

The table shows each Project name, your role in it, and how many triggers the Project has configured (a right-aligned `Triggers` column, including `0`); `ebs projects list --json` reports the same value as `trigger_count` on each item. Save a default after checking the exact name against the list:

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
