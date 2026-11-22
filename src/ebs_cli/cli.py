import os
import shlex
import sys
from collections.abc import Callable
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from rich.table import Table
from rich.text import Text

from ebs_cli import __version__
from ebs_cli.client import (
    EBSClient,
    normalize_server_url,
    resolve_trigger_reference,
    validate_cursor,
    validate_object_key,
    validate_prefix,
    validate_trigger_name,
)
from ebs_cli.config import (
    CONCURRENT_LOGIN_ERROR,
    DEFAULT_URL,
    config_path,
    load_globus_snapshot,
    load_saved_api_auth,
    load_token,
    optional_globus_credentials,
    read_auth_snapshot,
    save_auth_credentials,
    save_project,
    save_token,
    saved_project,
    validate_project_name,
)
from ebs_cli.config import logout as remove_token
from ebs_cli.errors import CLIError
from ebs_cli.models import Event, Project, S3Credentials, Trigger
from ebs_cli.native_auth import browser_login as native_browser_login
from ebs_cli.native_auth import refresh_login as native_refresh_login
from ebs_cli.native_auth import revoke_refresh_token
from ebs_cli.output import console, print_error, print_json, print_panel, safe_text, text
from ebs_cli.s3ops import delete_object, download_object, list_objects
from ebs_cli.security import redacted_headers
from ebs_cli.uploads import upload_file

app = typer.Typer(
    name="ebs",
    help="Work with NERSC Event Based Scheduling triggers, events, and S3 uploads.",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
auth_app = typer.Typer(help="Manage the personal EBS API token.", no_args_is_help=True)
projects_app = typer.Typer(help="Discover Projects and manage the saved default.", no_args_is_help=True)
triggers_app = typer.Typer(help="Inspect and control triggers.", no_args_is_help=True)
events_app = typer.Typer(help="Inspect events and their associated jobs.", no_args_is_help=True)
s3_app = typer.Typer(help="Get scoped S3 access and run direct object commands.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(projects_app, name="projects")
app.add_typer(triggers_app, name="triggers")
app.add_typer(events_app, name="events")
app.add_typer(s3_app, name="s3")

JSONOption = Annotated[bool, typer.Option("--json", help="Write stable JSON to stdout.")]
DurationOption = Annotated[
    int,
    typer.Option(
        "--duration-seconds", min=900, max=3600, help="Credential lifetime, from 900 to 3600 seconds."
    ),
]
TriggerRefArgument = Annotated[
    str,
    typer.Argument(
        metavar="TRIGGER_REF",
        help="Trigger ID or exact trigger name in the selected Project.",
    ),
]


class S3Operation(StrEnum):
    """Credential scopes the server can grant for one trigger's bucket."""

    UPLOAD = "upload"
    LIST = "list"
    DOWNLOAD = "download"
    DELETE = "delete"


PROJECT_SOURCE_FLAG = "flag"
PROJECT_SOURCE_ENVIRONMENT = "environment"
PROJECT_SOURCE_SAVED = "saved"
PROJECT_SOURCE_NONE = "none"

PROJECT_SOURCE_LABELS = {
    PROJECT_SOURCE_FLAG: "--project option",
    PROJECT_SOURCE_ENVIRONMENT: "EBS_PROJECT environment variable",
    PROJECT_SOURCE_SAVED: "saved default Project",
    PROJECT_SOURCE_NONE: "no default Project is set",
}


class Options:
    def __init__(self, url: str, project: str | None, project_source: str = PROJECT_SOURCE_NONE) -> None:
        self.url = url
        self.project = project
        self.project_source = project_source


def resolve_project(options: Options) -> tuple[str | None, str]:
    """Return the effective Project and its source, without contacting the API."""
    if options.project is not None:
        return options.project, options.project_source
    saved = saved_project()
    if saved is not None:
        return saved, PROJECT_SOURCE_SAVED
    return None, PROJECT_SOURCE_NONE


F = TypeVar("F", bound=Callable[..., Any])


def guarded(function: F) -> F:
    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except CLIError as exc:
            print_error(safe_text(exc, limit=700))
            raise typer.Exit(exc.exit_code) from None

    return wrapper  # type: ignore[return-value]


@app.callback()
def root(
    ctx: typer.Context,
    url: Annotated[
        str,
        typer.Option("--url", envvar="EBS_URL", show_envvar=True, help="EBS server origin."),
    ] = DEFAULT_URL,
    project: Annotated[
        str | None,
        typer.Option(
            "--project", "-p", envvar="EBS_PROJECT", show_envvar=True, help="Selected Project name."
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the CLI version and exit.", is_eager=True),
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    try:
        normalized_url = normalize_server_url(url)
    except CLIError as exc:
        print_error(str(exc))
        raise typer.Exit(exc.exit_code) from None
    parameter_source = ctx.get_parameter_source("project")
    source_name = parameter_source.name if parameter_source is not None else ""
    if source_name == "COMMANDLINE":
        project_source = PROJECT_SOURCE_FLAG
    elif source_name == "ENVIRONMENT":
        project_source = PROJECT_SOURCE_ENVIRONMENT
    else:
        project_source = PROJECT_SOURCE_NONE
    if project is not None:
        try:
            project = validate_project_name(project)
        except CLIError as exc:
            print_error(str(exc))
            raise typer.Exit(exc.exit_code) from None
    ctx.obj = Options(normalized_url, project, project_source)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def _normal_api_client(options: Options, project: str | None) -> EBSClient:
    """Use saved-login renewal only when no environment token overrides the file."""
    if os.environ.get("EBS_TOKEN") is not None:
        return EBSClient(options.url, load_token(), project)

    path = config_path()
    saved = load_saved_api_auth(path)
    renew_token: Callable[[], str] | None = None
    if saved.refresh_token is not None and saved.client_id is not None:
        refresh_token = saved.refresh_token
        client_id = saved.client_id

        def renew_saved_token() -> str:
            # Detect a replacement before spending the saved grant, then repeat
            # the same expected-snapshot check in the atomic storage transaction.
            if read_auth_snapshot(path) != saved.snapshot:
                raise CLIError(CONCURRENT_LOGIN_ERROR)
            credentials = native_refresh_login(options.url, refresh_token, client_id)
            save_auth_credentials(
                credentials.token,
                credentials.refresh_token,
                credentials.client_id,
                path,
                expected=saved.snapshot,
            )
            return credentials.token

        renew_token = renew_saved_token

    return EBSClient(options.url, saved.token, project, renew_token=renew_token)


def api_client(ctx: typer.Context) -> EBSClient:
    options = ctx.ensure_object(Options)
    project, _source = resolve_project(options)
    if project is None:
        raise CLIError(
            "No Project is selected. Run 'ebs projects list' to see your Projects and "
            "'ebs projects use PROJECT' to save a default, or pass --project or set EBS_PROJECT.",
            exit_code=2,
        )
    return _normal_api_client(options, project)


def _login_saved_message(path: Path, *, globus: bool) -> None:
    credential = "API token and Globus refresh credential" if globus else "API token"
    message = f"Saved {credential} with owner-only permissions."
    try:
        keeps_project = saved_project(path) is not None
    except CLIError:
        keeps_project = False
    if keeps_project:
        message += " The saved default Project remains."
    console.print(message, markup=False)


@auth_app.command(
    "login",
    # Capture unexpected arguments so Click cannot echo a mistakenly supplied
    # credential in its rich usage error; reject the whole invocation instead.
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
@guarded
def login(
    ctx: typer.Context,
    token: Annotated[
        bool,
        typer.Option("--token", help="Prompt for a personal token without opening a browser."),
    ] = False,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print the authorization URL and use a hidden manual code prompt."),
    ] = False,
) -> None:
    """Log in through Globus, or use --token for a hidden manual prompt."""
    if ctx.args:
        raise CLIError(
            "Unexpected login arguments were rejected. Never pass a credential on the command line; "
            "use 'ebs auth login --token' for a hidden interactive prompt.",
            exit_code=2,
        )
    if token:
        if no_browser:
            raise CLIError("--no-browser cannot be combined with --token.", exit_code=2)
        manual_token = typer.prompt("Personal EBS API token", hide_input=True)
        path = save_token(manual_token)
        _login_saved_message(path, globus=False)
        return
    options = ctx.ensure_object(Options)
    credentials = native_browser_login(
        options.url,
        no_browser=no_browser,
        show_url=lambda url: typer.echo(
            f"Open this URL in a browser on any device to authorize the EBS CLI:\n{url}\n"
            "After authorizing, copy the one-time code shown by Globus."
        ),
    )
    path = save_auth_credentials(
        credentials.token,
        credentials.refresh_token,
        credentials.client_id,
    )
    _login_saved_message(path, globus=True)


@auth_app.command("refresh")
@guarded
def refresh(ctx: typer.Context) -> None:
    """Rotate the saved personal token using the saved Globus grant."""
    expected, refresh_token, client_id = load_globus_snapshot()
    options = ctx.ensure_object(Options)
    credentials = native_refresh_login(
        options.url,
        refresh_token,
        client_id,
    )
    path = save_auth_credentials(
        credentials.token,
        credentials.refresh_token,
        credentials.client_id,
        expected=expected,
    )
    _login_saved_message(path, globus=True)


@auth_app.command("logout")
@guarded
def logout(
    local_only: Annotated[
        bool,
        typer.Option("--local-only", help="Clear local credentials without contacting Globus."),
    ] = False,
) -> None:
    """Clear saved login credentials, optionally revoking only the CLI grant."""
    expected = read_auth_snapshot()
    globus_credentials = optional_globus_credentials(snapshot=expected)
    revocation_confirmed = False
    revocation_attempted = globus_credentials is not None and not local_only
    if globus_credentials is not None and not local_only:
        try:
            revoke_refresh_token(*globus_credentials)
            revocation_confirmed = True
        except CLIError:
            # Local clearing must still work offline; never claim server logout.
            pass
    if remove_token(expected=expected):
        try:
            keeps_project = saved_project() is not None
        except CLIError:
            keeps_project = False
        message = "Removed the saved EBS and Globus login credentials from this device."
        if keeps_project:
            message += " The saved default Project remains."
        console.print(message, markup=False)
        if revocation_confirmed:
            console.print("Globus confirmed revocation of the saved CLI grant.", markup=False)
        elif revocation_attempted:
            console.print(
                "Globus revocation was not confirmed; the provider grant may remain active. "
                "Revoke it from your Globus account if needed.",
                markup=False,
            )
        elif local_only and globus_credentials is not None:
            console.print("Local-only logout did not revoke the Globus grant.", markup=False)
    else:
        console.print("No saved login credentials were present.")


@projects_app.command("list")
@guarded
def projects_list(ctx: typer.Context, json_output: JSONOption = False) -> None:
    """List the Projects your account can access, with per-Project trigger counts."""
    options = ctx.ensure_object(Options)
    with _normal_api_client(options, None) as client:
        projects = client.list_projects(include_trigger_counts=True)
        ordered = sorted(projects, key=lambda item: item.name)
        counted: list[tuple[Project, int]] = []
        for project in ordered:
            try:
                count = project.trigger_count
                if count is None:  # Compatibility with an API that predates batched counts.
                    count = client.count_triggers(project.name)
                counted.append((project, count))
            except CLIError as exc:
                raise CLIError(
                    f"Could not count triggers in Project '{project.name}'. {exc}",
                    exit_code=exc.exit_code,
                ) from None
    if json_output:
        print_json(
            {
                "items": [
                    {"name": project.name, "role": project.role, "trigger_count": count}
                    for project, count in counted
                ]
            }
        )
        return
    table = Table(title="Projects")
    table.add_column("Project")
    table.add_column("Role")
    table.add_column("Triggers", justify="right")
    for project, count in counted:
        table.add_row(text(project.name), text(project.role), text(count))
    console.print(table)
    if ordered:
        console.print(
            f"Showing {len(ordered)} Projects. Save a default with 'ebs projects use PROJECT'.",
            markup=False,
        )
    else:
        console.print("No Projects are associated with your account.")


@projects_app.command("use")
@guarded
def projects_use(
    ctx: typer.Context,
    project: Annotated[str, typer.Argument(help="Exact Project name from 'ebs projects list'.")],
) -> None:
    """Validate a Project name and save it as your default."""
    name = validate_project_name(project)
    options = ctx.ensure_object(Options)
    with _normal_api_client(options, None) as client:
        available = client.list_projects()
    if not any(candidate.name == name for candidate in available):
        raise CLIError(
            f"Unknown Project '{name}'. Run 'ebs projects list' to see your Projects.",
            exit_code=2,
        )
    save_project(name)
    console.print(f"Saved default Project '{name}'.", markup=False)


@projects_app.command("show")
@guarded
def projects_show(ctx: typer.Context, json_output: JSONOption = False) -> None:
    """Show the effective Project and its source, without contacting the API."""
    options = ctx.ensure_object(Options)
    project, source = resolve_project(options)
    if json_output:
        print_json({"project": project, "source": source})
        return
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    details.add_row(text("Effective Project"), text(project if project is not None else "(none)"))
    details.add_row(text("Source"), text(PROJECT_SOURCE_LABELS[source]))
    print_panel(details, title="Project selection")
    if project is None:
        console.print(
            "Run 'ebs projects list' to see your Projects and 'ebs projects use PROJECT' to save a default.",
            markup=False,
        )


@triggers_app.command("list")
@guarded
def triggers_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=100, help="Maximum triggers to fetch.")] = 50,
    offset: Annotated[int, typer.Option(min=0, help="Number of triggers to skip.")] = 0,
    enabled: Annotated[
        bool | None, typer.Option("--enabled/--disabled", help="Filter by trigger state.")
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(help="Only triggers whose name contains this text, case-insensitively."),
    ] = None,
    json_output: JSONOption = False,
) -> None:
    """List one bounded page of triggers in the selected Project."""
    name_filter = validate_trigger_name(name) if name is not None else None
    with api_client(ctx) as client:
        request: dict[str, Any] = {"limit": limit, "offset": offset, "enabled": enabled}
        if name_filter is not None:
            request["name"] = name_filter
        page = client.list_triggers(**request)
    if json_output:
        print_json(page.model_dump(mode="json"))
        return
    table = Table(title="Triggers")
    for column in ("ID", "Name", "Version", "Enabled", "Input", "Target", "Project"):
        table.add_column(column)
    for trigger in page.items:
        table.add_row(
            text(trigger.id),
            text(trigger.name),
            text(trigger.version or "-"),
            text("yes" if trigger.enabled else "no"),
            text(trigger.pipeline.get("input_type", "-")),
            text(trigger.machine or "-"),
            text(trigger.group_name or trigger.group_id),
        )
    console.print(table)
    first = page.offset + 1 if page.items else 0
    last = page.offset + len(page.items)
    summary = f"Showing {first}-{last} of {page.total}."
    if last < page.total:
        summary += f" Use --offset {last} for the next page."
    console.print(summary)


def _event_json(event: Event) -> dict[str, Any]:
    body = event.model_dump(mode="json")
    if event.job is not None:
        body["job"]["error"] = safe_text(event.job.error, limit=500) if event.job.error else None
    return body


def _trigger_json(trigger: Trigger) -> dict[str, Any]:
    return trigger.model_dump(mode="json")


def _show_trigger(trigger: Trigger, *, heading: str = "Trigger") -> None:
    details = Table.grid(padding=(0, 2))
    details.add_column(style="bold")
    details.add_column()
    rows = (
        ("ID", trigger.id),
        ("Name", trigger.name),
        ("Version", trigger.version or "-"),
        ("Project", trigger.group_name or trigger.group_id),
        ("Enabled", "yes" if trigger.enabled else "no"),
        ("Input", trigger.pipeline.get("input_type", "-")),
        ("Target", trigger.machine or "-"),
        ("Updated", trigger.updated_at),
    )
    for label, value in rows:
        details.add_row(text(label), text(value))
    if trigger.instance is not None:
        details.add_row(text("Instance"), text(trigger.instance.status))
        if trigger.instance.failure_reason:
            details.add_row(text("Failure"), text(trigger.instance.failure_reason, limit=500))
    print_panel(details, title=heading)


@triggers_app.command("show")
@guarded
def triggers_show(
    ctx: typer.Context, trigger_ref: TriggerRefArgument, json_output: JSONOption = False
) -> None:
    """Show one trigger by ID or exact name."""
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        trigger = client.get_trigger(trigger_id)
    if json_output:
        print_json(_trigger_json(trigger))
    else:
        _show_trigger(trigger)


def _set_enabled(ctx: typer.Context, trigger_ref: str, enabled: bool, json_output: bool) -> None:
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        trigger = client.set_trigger_enabled(trigger_id, enabled)
    if json_output:
        print_json(_trigger_json(trigger))
    else:
        _show_trigger(trigger, heading="Trigger enabled" if enabled else "Trigger disabled")


@triggers_app.command("enable")
@guarded
def triggers_enable(
    ctx: typer.Context, trigger_ref: TriggerRefArgument, json_output: JSONOption = False
) -> None:
    """Enable a trigger by ID or exact name and start its service."""
    _set_enabled(ctx, trigger_ref, True, json_output)


@triggers_app.command("disable")
@guarded
def triggers_disable(
    ctx: typer.Context, trigger_ref: TriggerRefArgument, json_output: JSONOption = False
) -> None:
    """Disable a trigger by ID or exact name and stop its service."""
    _set_enabled(ctx, trigger_ref, False, json_output)


@triggers_app.command("delete")
@guarded
def triggers_delete(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the interactive confirmation.")] = False,
    json_output: JSONOption = False,
) -> None:
    """Delete a trigger by ID or exact name, together with its event and job history."""
    if not yes and not _stdin_is_interactive():
        raise CLIError("Pass --yes to delete a trigger from a noninteractive session.", exit_code=2)
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        trigger = client.get_trigger(trigger_id)
        if not json_output:
            project_label = trigger.group_name or trigger.group_id
            console.print(
                f"Trigger '{trigger.name}' (ID {trigger.id}) in Project {project_label}.",
                markup=False,
            )
        if not yes:
            confirmed = typer.confirm(
                "Permanently delete this trigger and its event and job history?",
                default=False,
                err=json_output,
            )
            if not confirmed:
                if json_output:
                    print_error("Deletion cancelled. Nothing was deleted.")
                else:
                    console.print("Deletion cancelled. Nothing was deleted.", markup=False)
                raise typer.Exit(1)
        client.delete_trigger(trigger_id)
    if json_output:
        print_json(_trigger_json(trigger))
    else:
        _show_trigger(trigger, heading="Trigger deleted")


@events_app.command("list")
@guarded
def events_list(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Maximum events to fetch.")] = 50,
    cursor: Annotated[str | None, typer.Option(help="Opaque cursor returned by an earlier page.")] = None,
    status: Annotated[str | None, typer.Option(help="Filter by event status.")] = None,
    source: Annotated[str | None, typer.Option(help="Filter by event source.")] = None,
    json_output: JSONOption = False,
) -> None:
    """List one bounded page of events and associated jobs for a trigger."""
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        page = client.list_events(trigger_id, limit=limit, cursor=cursor, status=status, source=source)
    if json_output:
        body = page.model_dump(mode="json")
        body["items"] = [_event_json(event) for event in page.items]
        print_json(body)
        return
    table = Table(title=Text(f"Events for {trigger_ref}"))
    for column in ("Received", "Event ID", "Status", "Source", "Job ID", "Job status", "Failure"):
        table.add_column(column)
    for event in page.items:
        job = event.job
        table.add_row(
            text(event.received_at),
            text(event.id),
            text(event.status),
            text(event.source),
            text(job.id if job else "-"),
            text(job.status if job else "-"),
            text(job.error or "-" if job else "-", limit=500),
        )
    console.print(table)
    if page.has_more and page.next_cursor:
        cursor = safe_text(page.next_cursor, limit=1024)
        console.print(f"More events are available. Use --cursor {cursor}", markup=False)
    else:
        console.print("No more events in this result set.")


def _credential_exports(credentials: S3Credentials) -> list[str]:
    return [
        f"export AWS_ACCESS_KEY_ID={shlex.quote(credentials.access_key_id)}",
        f"export AWS_SECRET_ACCESS_KEY={shlex.quote(credentials.secret_access_key)}",
        f"export AWS_SESSION_TOKEN={shlex.quote(credentials.session_token)}",
        f"export AWS_DEFAULT_REGION={shlex.quote(credentials.region)}",
        f"export EBS_S3_ENDPOINT={shlex.quote(credentials.endpoint_url)}",
    ]


def _python_instructions(credentials: S3Credentials, operation: S3Operation) -> list[str]:
    bucket = credentials.bucket
    lines = [
        "import boto3",
        "from botocore.config import Config",
        "",
        "s3 = boto3.client(",
        '    "s3",',
        f"    endpoint_url={credentials.endpoint_url!r},",
        f"    region_name={credentials.region!r},",
        f"    aws_access_key_id={credentials.access_key_id!r},",
        f"    aws_secret_access_key={credentials.secret_access_key!r},",
        f"    aws_session_token={credentials.session_token!r},",
        "    config=Config(",
        '        signature_version="s3v4",',
        '        s3={"addressing_style": "path"},',
        "        connect_timeout=10,",
        "        read_timeout=120,",
        "    ),",
        ")",
    ]
    if operation is S3Operation.UPLOAD:
        lines.append(f's3.upload_file("./FILE", {bucket!r}, "KEY")')
    elif operation is S3Operation.LIST:
        lines.extend(
            (
                f"response = s3.list_objects_v2(Bucket={bucket!r}, MaxKeys=100)",
                'for item in response.get("Contents", []):',
                '    print(item["Key"], item["Size"], item["LastModified"])',
            )
        )
    elif operation is S3Operation.DOWNLOAD:
        lines.extend(
            (
                f'response = s3.get_object(Bucket={bucket!r}, Key="KEY")',
                'with open("./FILE", "wb") as local_file:',
                '    for chunk in response["Body"].iter_chunks():',
                "        local_file.write(chunk)",
            )
        )
    else:
        lines.append(f's3.delete_object(Bucket={bucket!r}, Key="KEY")')
    return lines


@s3_app.command("credentials")
@guarded
def s3_credentials(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    duration_seconds: DurationOption = 900,
    operation: Annotated[
        S3Operation,
        typer.Option("--operation", help="Credential scope: upload, list, download, or delete."),
    ] = S3Operation.UPLOAD,
    json_output: JSONOption = False,
    shell: Annotated[
        bool, typer.Option("--shell", help="Write temporary credential environment exports.")
    ] = False,
) -> None:
    """Request temporary credentials scoped to the trigger's bucket."""
    if json_output and shell:
        raise CLIError("Choose either --json or --shell, not both.", exit_code=2)
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        credentials = client.s3_credentials(
            trigger_id, duration_seconds=duration_seconds, operation=operation.value
        )
    if json_output:
        print_json(credentials.model_dump(mode="json"))
        return
    if shell:
        for line in _credential_exports(credentials):
            typer.echo(line)
        return
    lines = _python_instructions(credentials, operation)
    lines.extend(
        (
            f"# Expires: {credentials.expires_at.isoformat()}",
            "# Credentials are scoped to this trigger's bucket.",
        )
    )
    print_panel("\n".join(lines), title=f"Temporary {operation.value} credentials for {credentials.bucket}")


@s3_app.command("upload-url")
@guarded
def s3_upload_url(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    key: str,
    duration_seconds: DurationOption = 900,
    show_url: Annotated[
        bool,
        typer.Option("--show-url", help="Reveal the signed URL, which is a temporary bearer credential."),
    ] = False,
    json_output: JSONOption = False,
) -> None:
    """Create a presigned PUT URL for one object."""
    object_key = validate_object_key(key)
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        upload = client.s3_upload_url(trigger_id, object_key, duration_seconds=duration_seconds)
    displayed_url = upload.url if show_url else "[signed URL redacted]"
    headers = redacted_headers(upload.headers, reveal=show_url)
    if json_output:
        body = upload.model_dump(mode="json")
        body["url"] = displayed_url
        body["headers"] = headers
        print_json(body)
        return
    command = ["curl", "--fail", "--request", "PUT", "--upload-file", f"./{upload.key}"]
    for name, value in headers.items():
        command.extend(("--header", f"{name}: {value}"))
    command.append(displayed_url)
    lines = [
        f"Target: s3://{upload.bucket}/{upload.key}",
        f"Method: {upload.method}",
        f"Expires: {upload.expires_at.isoformat()}",
        shlex.join(command),
    ]
    if not show_url:
        lines.append("The signed URL is redacted. Use --show-url only when you intend to expose it.")
    print_panel("\n".join(lines), title="One-object upload URL")


@s3_app.command("upload")
@guarded
def s3_upload(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    file: Annotated[Path, typer.Argument(help="Local file to stream directly to S3.")],
    key: Annotated[str | None, typer.Option(help="Object key. Defaults to the local file name.")] = None,
    duration_seconds: DurationOption = 900,
    json_output: JSONOption = False,
) -> None:
    """Upload a file directly to the trigger's bucket with scoped temporary credentials."""
    try:
        is_file = file.is_file()
    except OSError:
        is_file = False
    if not is_file:
        raise CLIError(f"Local file does not exist or is not a regular file: {file}", exit_code=2)
    object_key = validate_object_key(key if key is not None else file.name)
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        credentials = client.s3_credentials(
            trigger_id, duration_seconds=duration_seconds, operation=S3Operation.UPLOAD.value
        )
    upload_file(credentials, file, object_key)
    target = f"s3://{credentials.bucket}/{object_key}"
    if json_output:
        print_json({"bucket": credentials.bucket, "key": object_key, "status": "uploaded", "target": target})
    else:
        console.print(f"Uploaded {file.name} to {target}.", markup=False)


def _validate_download_destination(destination: Path, *, force: bool) -> None:
    """Reject unsafe local destinations before any credential exchange."""
    parent = destination.parent
    if not parent.is_dir():
        raise CLIError(f"Destination directory does not exist: {parent}", exit_code=2)
    if destination.is_symlink():
        raise CLIError(
            f"Destination is a symbolic link; refusing to write through it: {destination}", exit_code=2
        )
    try:
        exists = destination.exists()
    except OSError:
        exists = False
    if not exists:
        return
    if destination.is_dir():
        raise CLIError(f"Destination is a directory: {destination}", exit_code=2)
    if not force:
        raise CLIError(f"Destination already exists. Use --force to overwrite it: {destination}", exit_code=2)


def _stdin_is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError, AttributeError):
        return False


@s3_app.command("ls")
@guarded
def s3_ls(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    prefix: Annotated[str | None, typer.Option(help="Only keys that begin with this prefix.")] = None,
    limit: Annotated[
        int, typer.Option(min=1, max=1000, help="Maximum objects to fetch, from 1 to 1000.")
    ] = 100,
    cursor: Annotated[
        str | None, typer.Option(help="Opaque continuation token from an earlier page.")
    ] = None,
    json_output: JSONOption = False,
) -> None:
    """List one bounded page of objects in the trigger's bucket."""
    prefix_text = validate_prefix(prefix or "")
    cursor_text = validate_cursor(cursor) if cursor is not None else None
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        credentials = client.s3_credentials(
            trigger_id, duration_seconds=900, operation=S3Operation.LIST.value
        )
    page = list_objects(credentials, prefix=prefix_text, limit=limit, cursor=cursor_text)
    if json_output:
        body: dict[str, Any] = {
            "bucket": credentials.bucket,
            "limit": limit,
            "prefix": prefix_text,
            "items": page["items"],
        }
        if page["next_cursor"]:
            body["next_cursor"] = page["next_cursor"]
        print_json(body)
        return
    title = Text(f"Objects in s3://{credentials.bucket}")
    if prefix_text:
        title.append(f" under {prefix_text}")
    table = Table(title=title)
    for column in ("Key", "Size", "Last modified"):
        table.add_column(column)
    for item in page["items"]:
        table.add_row(text(item["key"]), text(item["size"]), text(item["last_modified"]))
    console.print(table)
    if page["items"]:
        if page["next_cursor"]:
            token = safe_text(page["next_cursor"], limit=1024)
            console.print(f"More results are available. Use --cursor {token}", markup=False)
        else:
            console.print("No more results in this result set.")
    else:
        console.print("No objects matched this listing.")


@s3_app.command("download")
@guarded
def s3_download(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    key: Annotated[str, typer.Argument(help="Object key in the trigger's bucket.")],
    file: Annotated[Path, typer.Argument(help="Local destination path for the object.")],
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing regular file at the destination.")
    ] = False,
    json_output: JSONOption = False,
) -> None:
    """Download one object to a local file with scoped temporary credentials."""
    object_key = validate_object_key(key)
    _validate_download_destination(file, force=force)
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        credentials = client.s3_credentials(
            trigger_id, duration_seconds=900, operation=S3Operation.DOWNLOAD.value
        )
    download_object(credentials, object_key, file, force=force)
    target = f"s3://{credentials.bucket}/{object_key}"
    if json_output:
        print_json(
            {
                "bucket": credentials.bucket,
                "key": object_key,
                "path": str(file),
                "status": "downloaded",
                "target": target,
            }
        )
    else:
        console.print(f"Downloaded {target} to {file}.", markup=False)


@s3_app.command("rm")
@guarded
def s3_rm(
    ctx: typer.Context,
    trigger_ref: TriggerRefArgument,
    key: Annotated[str, typer.Argument(help="Exact object key to delete.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the interactive confirmation.")] = False,
    json_output: JSONOption = False,
) -> None:
    """Delete exactly one object from the trigger's bucket after confirmation."""
    object_key = validate_object_key(key)
    if not yes:
        if not _stdin_is_interactive():
            raise CLIError("Pass --yes to delete from a noninteractive session.", exit_code=2)
        confirmed = typer.confirm(
            f"Permanently delete {object_key!r} from trigger {trigger_ref}'s bucket?", default=False
        )
        if not confirmed:
            console.print("Deletion cancelled. Nothing was deleted.", markup=False)
            raise typer.Exit(1)
    with api_client(ctx) as client:
        trigger_id = resolve_trigger_reference(client, trigger_ref)
        credentials = client.s3_credentials(
            trigger_id, duration_seconds=900, operation=S3Operation.DELETE.value
        )
    delete_object(credentials, object_key)
    target = f"s3://{credentials.bucket}/{object_key}"
    if json_output:
        print_json({"bucket": credentials.bucket, "key": object_key, "status": "deleted", "target": target})
    else:
        console.print(f"Deleted {target}.", markup=False)


def main() -> None:
    try:
        app()
    except Exception:
        # Do not turn unexpected request/config exceptions into secret-bearing tracebacks.
        print_error("Unexpected CLI failure. No request or credential details were displayed.")
        raise SystemExit(1) from None
