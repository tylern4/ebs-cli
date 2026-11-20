import shlex
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, TypeVar
from uuid import UUID

import typer
from rich.table import Table

from ebs_cli import __version__
from ebs_cli.client import EBSClient, normalize_server_url, validate_object_key
from ebs_cli.config import DEFAULT_URL, load_token, save_token
from ebs_cli.config import logout as remove_token
from ebs_cli.errors import CLIError
from ebs_cli.models import Event, S3Credentials, Trigger
from ebs_cli.output import console, print_error, print_json, print_panel, safe_text, text
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
triggers_app = typer.Typer(help="Inspect and control triggers.", no_args_is_help=True)
events_app = typer.Typer(help="Inspect events and their associated jobs.", no_args_is_help=True)
s3_app = typer.Typer(help="Get scoped S3 upload access.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
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


class Options:
    def __init__(self, url: str, project: str | None) -> None:
        self.url = url
        self.project = project


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
    if project is not None:
        project = project.strip()
        if not project or not project.isascii() or any(not character.isprintable() for character in project):
            print_error("Project must be a non-empty printable ASCII name.")
            raise typer.Exit(2)
    ctx.obj = Options(normalized_url, project)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def api_client(ctx: typer.Context) -> EBSClient:
    options = ctx.ensure_object(Options)
    if options.project is None:
        raise CLIError("Select a Project with --project or EBS_PROJECT.", exit_code=2)
    return EBSClient(options.url, load_token(), options.project)


@auth_app.command("login")
@guarded
def login() -> None:
    """Save a personal API token in an owner-only config file."""
    token = typer.prompt("Personal EBS API token", hide_input=True)
    path = save_token(token)
    console.print(f"Saved API token to {path} with owner-only permissions.", markup=False)


@auth_app.command("logout")
@guarded
def logout() -> None:
    """Remove the saved API token from the user config file."""
    if remove_token():
        console.print("Removed the saved API token.")
    else:
        console.print("No saved API token was present.")


@triggers_app.command("list")
@guarded
def triggers_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option(min=1, max=100, help="Maximum triggers to fetch.")] = 50,
    offset: Annotated[int, typer.Option(min=0, help="Number of triggers to skip.")] = 0,
    enabled: Annotated[
        bool | None, typer.Option("--enabled/--disabled", help="Filter by trigger state.")
    ] = None,
    json_output: JSONOption = False,
) -> None:
    """List one bounded page of triggers in the selected Project."""
    with api_client(ctx) as client:
        page = client.list_triggers(limit=limit, offset=offset, enabled=enabled)
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
def triggers_show(ctx: typer.Context, trigger_id: UUID, json_output: JSONOption = False) -> None:
    """Show one trigger."""
    with api_client(ctx) as client:
        trigger = client.get_trigger(trigger_id)
    if json_output:
        print_json(_trigger_json(trigger))
    else:
        _show_trigger(trigger)


def _set_enabled(ctx: typer.Context, trigger_id: UUID, enabled: bool, json_output: bool) -> None:
    with api_client(ctx) as client:
        trigger = client.set_trigger_enabled(trigger_id, enabled)
    if json_output:
        print_json(_trigger_json(trigger))
    else:
        _show_trigger(trigger, heading="Trigger enabled" if enabled else "Trigger disabled")


@triggers_app.command("enable")
@guarded
def triggers_enable(ctx: typer.Context, trigger_id: UUID, json_output: JSONOption = False) -> None:
    """Enable a trigger and start its service."""
    _set_enabled(ctx, trigger_id, True, json_output)


@triggers_app.command("disable")
@guarded
def triggers_disable(ctx: typer.Context, trigger_id: UUID, json_output: JSONOption = False) -> None:
    """Disable a trigger and stop its service."""
    _set_enabled(ctx, trigger_id, False, json_output)


@events_app.command("list")
@guarded
def events_list(
    ctx: typer.Context,
    trigger_id: UUID,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Maximum events to fetch.")] = 50,
    cursor: Annotated[str | None, typer.Option(help="Opaque cursor returned by an earlier page.")] = None,
    status: Annotated[str | None, typer.Option(help="Filter by event status.")] = None,
    source: Annotated[str | None, typer.Option(help="Filter by event source.")] = None,
    json_output: JSONOption = False,
) -> None:
    """List one bounded page of events and associated jobs."""
    with api_client(ctx) as client:
        page = client.list_events(trigger_id, limit=limit, cursor=cursor, status=status, source=source)
    if json_output:
        body = page.model_dump(mode="json")
        body["items"] = [_event_json(event) for event in page.items]
        print_json(body)
        return
    table = Table(title=f"Events for {trigger_id}")
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


def _python_instructions(credentials: S3Credentials) -> list[str]:
    return [
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
        f's3.upload_file("./FILE", {credentials.bucket!r}, "KEY")',
    ]


@s3_app.command("credentials")
@guarded
def s3_credentials(
    ctx: typer.Context,
    trigger_id: UUID,
    duration_seconds: DurationOption = 900,
    json_output: JSONOption = False,
    shell: Annotated[
        bool, typer.Option("--shell", help="Write temporary credential environment exports.")
    ] = False,
) -> None:
    """Request temporary credentials scoped to the trigger's bucket."""
    if json_output and shell:
        raise CLIError("Choose either --json or --shell, not both.", exit_code=2)
    with api_client(ctx) as client:
        credentials = client.s3_credentials(trigger_id, duration_seconds=duration_seconds)
    if json_output:
        print_json(credentials.model_dump(mode="json"))
        return
    if shell:
        for line in _credential_exports(credentials):
            typer.echo(line)
        return
    lines = _python_instructions(credentials)
    lines.extend(
        (
            f"# Expires: {credentials.expires_at.isoformat()}",
            "# Credentials are scoped to this trigger's bucket.",
        )
    )
    print_panel("\n".join(lines), title=f"Temporary upload credentials for {credentials.bucket}")


@s3_app.command("upload-url")
@guarded
def s3_upload_url(
    ctx: typer.Context,
    trigger_id: UUID,
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
    trigger_id: UUID,
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
        credentials = client.s3_credentials(trigger_id, duration_seconds=duration_seconds)
    upload_file(credentials, file, object_key)
    target = f"s3://{credentials.bucket}/{object_key}"
    if json_output:
        print_json({"bucket": credentials.bucket, "key": object_key, "status": "uploaded", "target": target})
    else:
        console.print(f"Uploaded {file.name} to {target}.", markup=False)


def main() -> None:
    try:
        app()
    except Exception:
        # Do not turn unexpected request/config exceptions into secret-bearing tracebacks.
        print_error("Unexpected CLI failure. No request or credential details were displayed.")
        raise SystemExit(1) from None
