"""Strict JSON/YAML authoring documents for trigger create, apply, and export."""

import copy
import json
import sys
from pathlib import Path
from typing import Annotated, Any, BinaryIO, Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ebs_cli.client import NAME_LOOKUP_LIMIT, EBSClient
from ebs_cli.errors import CLIError
from ebs_cli.models import SfapiTokenMetadata, Trigger

MAX_DOCUMENT_BYTES = 1024 * 1024
_DOCUMENT_SUFFIXES = {".json": "json", ".yaml": "yaml", ".yml": "yaml"}


class DocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class JobSpecDocument(DocumentModel):
    # A scheduler-hosted path or an inline script, never a local upload. The
    # response-only derived machine deliberately has no authoring field.
    source: Literal["path", "template"]
    path: str | None = None
    template: str | None = Field(default=None, max_length=MAX_DOCUMENT_BYTES)

    @model_validator(mode="after")
    def validate_source(self) -> "JobSpecDocument":
        if self.source == "path":
            if self.path is None or not self.path.strip():
                raise ValueError("path is required when source is 'path'")
            if self.template is not None and self.template.strip():
                raise ValueError(
                    "job_spec carries both path and template; keep only the source='path' content"
                )
            return self
        if self.template is None or not self.template.strip():
            raise ValueError("template must be nonblank when source is 'template'")
        if self.path is not None and self.path.strip():
            raise ValueError(
                "job_spec carries both path and template; keep only the source='template' content"
            )
        return self


class StartJobOutputDocument(DocumentModel):
    mode: Literal["start_job"]
    job_spec: JobSpecDocument


class CallbackOutputDocument(DocumentModel):
    mode: Literal["callback"]
    url: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_url(self) -> "CallbackOutputDocument":
        if not self.url.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return self


PipelineOutputDocument = Annotated[
    StartJobOutputDocument | CallbackOutputDocument,
    Field(discriminator="mode"),
]


class MatchersDocument(DocumentModel):
    event_type: str | None = None
    s3_events: list[str] | None = None
    s3_bucket: str | None = None
    path_prefix: str | None = None
    path_glob: str | None = None
    file_types: list[str] | None = None
    job_states: list[str] | None = None
    job_name: str | None = None
    job_user: str | None = None
    job_account: str | None = None
    user: str | None = None
    group: str | None = None
    raw_yaml: str | None = None


class PipelineDocument(DocumentModel):
    input_type: Literal["s3_bucket_events", "job_events"]
    matchers: MatchersDocument
    outputs: PipelineOutputDocument


class TriggerDocument(DocumentModel):
    """The complete user-editable state; never a partial update manifest."""

    name: str = Field(min_length=1, max_length=255)
    project: str = Field(min_length=1, max_length=255)
    version: str = Field(default="v1", max_length=64)
    enabled: bool = True
    machine: str | None = None
    sfapi_username: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^\S+$")
    sfapi_account: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^\S+$")
    pipeline: PipelineDocument

    @model_validator(mode="after")
    def validate_binding(self) -> "TriggerDocument":
        if self.sfapi_account is not None and self.sfapi_username is None:
            raise ValueError("sfapi_username is required when sfapi_account is set")
        if self.pipeline.outputs.mode == "callback" and (
            self.sfapi_username is not None or self.machine is not None
        ):
            raise ValueError("callback documents cannot set machine or SFAPI credential fields")
        return self


class _StrictYAMLLoader(yaml.SafeLoader):
    """Safe YAML plus duplicate-key rejection and JSON-style string keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise CLIError("Invalid YAML document: expected an object.", exit_code=2)
        keys: set[str] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise CLIError("Invalid YAML document: object keys must be strings.", exit_code=2)
            if key in keys:
                raise CLIError(
                    "Invalid trigger document: duplicate object keys are not allowed.", exit_code=2
                )
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


class _EditableYAML(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _represent_string(dumper: _EditableYAML, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_EditableYAML.add_representer(str, _represent_string)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CLIError("Invalid trigger document: duplicate object keys are not allowed.", exit_code=2)
        result[key] = value
    return result


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise CLIError("Trigger document must be UTF-8 text.", exit_code=2) from None


def _parse_yaml(text: str) -> Any:
    try:
        documents = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.events.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise CLIError("Trigger document must contain exactly one YAML document.", exit_code=2)
            elif isinstance(event, yaml.events.AliasEvent):
                raise CLIError("YAML aliases and merge shortcuts are not allowed.", exit_code=2)
            elif isinstance(event, yaml.events.ScalarEvent) and (
                event.tag == "tag:yaml.org,2002:merge" or (event.tag is None and event.value == "<<")
            ):
                raise CLIError("YAML aliases and merge shortcuts are not allowed.", exit_code=2)
        return yaml.load(text, Loader=_StrictYAMLLoader)  # noqa: S506 - SafeLoader subclass
    except CLIError:
        raise
    except (yaml.YAMLError, RecursionError):
        raise CLIError("Invalid or unsafe YAML trigger document.", exit_code=2) from None


def parse_trigger_document(content: bytes, syntax: str) -> TriggerDocument:
    """Parse and validate one bounded-syntax document without showing its values."""
    text = _decode(content)
    try:
        if syntax == "json":
            raw = json.loads(
                text,
                object_pairs_hook=_json_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite number")),
            )
        elif syntax == "yaml":
            raw = _parse_yaml(text)
        else:  # Internal callers pass only extension- or option-validated values.
            raise CLIError("Document format must be json or yaml.", exit_code=2)
    except CLIError:
        raise
    except (ValueError, RecursionError):
        raise CLIError(f"Invalid {syntax.upper()} trigger document syntax.", exit_code=2) from None
    if not isinstance(raw, dict):
        raise CLIError("Trigger document root must be one object.", exit_code=2)
    try:
        return TriggerDocument.model_validate(raw)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_url=False, include_input=False)[:6]:
            location = error["loc"]
            if error["type"] == "extra_forbidden" and location:
                # An unknown object's final loc component is its authored key,
                # not a trusted schema name. Keep only its known parent path.
                location = location[:-1]
            path = ".".join(str(part) for part in location) or "document"
            message = (
                "mode must be one of: start_job, callback"
                if error["type"] == "union_tag_invalid"
                else error["msg"]
            )
            errors.append(f"{path}: {message}")
        suffix = "; ".join(errors)
        if len(exc.errors()) > 6:
            suffix += "; additional validation errors omitted"
        raise CLIError(f"Invalid trigger document. {suffix}", exit_code=2) from None


def read_trigger_document(
    file: str,
    syntax: str | None = None,
    *,
    stdin: BinaryIO | None = None,
) -> TriggerDocument:
    """Read at most 1 MiB, rejecting file and syntax failures before any API call."""
    if file == "-":
        if syntax not in {"json", "yaml"}:
            raise CLIError("Standard input requires --format json or --format yaml.", exit_code=2)
        stream = stdin if stdin is not None else sys.stdin.buffer
        try:
            content = stream.read(MAX_DOCUMENT_BYTES + 1)
        except (OSError, ValueError):
            raise CLIError("Could not read the trigger document from standard input.", exit_code=2) from None
    else:
        extension_syntax = _DOCUMENT_SUFFIXES.get(Path(file).suffix.lower())
        if extension_syntax is None:
            raise CLIError("Trigger document file must end in .json, .yaml, or .yml.", exit_code=2)
        if syntax is not None and syntax != extension_syntax:
            raise CLIError("--format must match the trigger document file extension.", exit_code=2)
        try:
            with Path(file).open("rb") as source:
                content = source.read(MAX_DOCUMENT_BYTES + 1)
        except (OSError, ValueError):
            raise CLIError("Could not read the trigger document file.", exit_code=2) from None
    if len(content) > MAX_DOCUMENT_BYTES:
        raise CLIError("Trigger document exceeds the 1 MiB size limit.", exit_code=2)
    return parse_trigger_document(content, extension_syntax if file != "-" else syntax)


def canonical_pipeline(
    pipeline: PipelineDocument | dict[str, Any], *, response: bool = False
) -> dict[str, Any]:
    """Return JSON-compatible canonical pipeline state without derived machine."""
    raw = copy.deepcopy(pipeline if isinstance(pipeline, dict) else pipeline.model_dump(mode="json"))
    if response and isinstance(raw, dict):
        outputs = raw.get("outputs")
        if isinstance(outputs, dict):
            job_spec = outputs.get("job_spec")
            if isinstance(job_spec, dict):
                job_spec.pop("machine", None)
    try:
        parsed = raw if isinstance(raw, PipelineDocument) else PipelineDocument.model_validate(raw)
    except ValidationError:
        if response:
            raise CLIError("Server returned a trigger pipeline outside the expected API format.") from None
        raise CLIError("Invalid trigger pipeline.", exit_code=2) from None
    result = parsed.model_dump(mode="json", exclude_none=True)
    outputs = result["outputs"]
    job_spec = outputs.get("job_spec")
    if job_spec is not None:
        # Empty, irrelevant sibling fields are absent in API responses.
        if job_spec["source"] == "path":
            job_spec.pop("template", None)
        else:
            job_spec.pop("path", None)
    return result


def canonical_scheduler_account(accounts: list[str], selected: str) -> str:
    """Match core's one-leading-c_ Account normalization and collision rules."""
    mapping: dict[str, str] = {}
    for raw in accounts:
        canonical = raw[2:] if raw.startswith("c_") else raw
        if not canonical or len(canonical) > 255 or any(character.isspace() for character in canonical):
            raise CLIError("The selected Account is unavailable or ambiguous for this Username.", exit_code=2)
        previous = mapping.get(canonical)
        if previous is not None and previous != raw:
            raise CLIError("The selected Account is unavailable or ambiguous for this Username.", exit_code=2)
        mapping[canonical] = raw
    matches = {selected} if selected in mapping else set()
    matches.update(canonical for canonical, raw in mapping.items() if raw == selected)
    if len(matches) != 1:
        raise CLIError("The requested Account is absent or ambiguous for this Username.", exit_code=2)
    return matches.pop()


def resolve_document_binding(
    document: TriggerDocument,
    credentials: list[SfapiTokenMetadata],
) -> tuple[UUID | None, str | None]:
    if document.sfapi_username is None:
        return None, None
    matches = [item for item in credentials if item.sfapi_username == document.sfapi_username]
    if len(matches) != 1:
        reason = "ambiguous" if matches else "missing or inaccessible"
        raise CLIError(f"SFAPI Username is {reason}; exactly one accessible match is required.", exit_code=2)
    account = document.sfapi_account
    if account is not None:
        account = canonical_scheduler_account(matches[0].accounts, account)
    return matches[0].token_ref, account


def resolve_export_binding(
    trigger: Trigger,
    credentials: list[SfapiTokenMetadata],
) -> tuple[str | None, str | None]:
    if trigger.sfapi_token_ref is None:
        if trigger.sfapi_account is not None:
            raise CLIError("Trigger has an Account without a credential binding and cannot be exported.")
        return None, None
    matches = [item for item in credentials if item.token_ref == trigger.sfapi_token_ref]
    if len(matches) != 1:
        reason = "ambiguous" if matches else "missing or inaccessible"
        raise CLIError(
            f"The trigger's SFAPI credential metadata is {reason}; cannot export an editable document."
        )
    account = trigger.sfapi_account
    if account is not None:
        account = canonical_scheduler_account(matches[0].accounts, account)
    return matches[0].sfapi_username, account


def desired_state(
    document: TriggerDocument,
    token_ref: UUID | None,
    account: str | None,
) -> dict[str, Any]:
    """Canonical full mutable state shared by PATCH and no-op comparison."""
    return {
        "name": document.name,
        "version": document.version,
        "enabled": document.enabled,
        "machine": document.machine,
        "sfapi_token_ref": str(token_ref) if token_ref is not None else None,
        "sfapi_account": account,
        "pipeline": canonical_pipeline(document.pipeline),
    }


def current_state(trigger: Trigger) -> dict[str, Any]:
    return {
        "name": trigger.name,
        "version": trigger.version,
        "enabled": trigger.enabled,
        "machine": trigger.machine,
        "sfapi_token_ref": str(trigger.sfapi_token_ref) if trigger.sfapi_token_ref is not None else None,
        "sfapi_account": trigger.sfapi_account,
        "pipeline": canonical_pipeline(trigger.pipeline, response=True),
    }


def create_payload(
    document: TriggerDocument,
    token_ref: UUID | None,
    account: str | None,
) -> dict[str, Any]:
    """The user API accepts the exact Project name as its transport 'group'."""
    return {"group": document.project, **desired_state(document, token_ref, account)}


def export_document(
    trigger: Trigger,
    project: str,
    credentials: list[SfapiTokenMetadata],
) -> TriggerDocument:
    username, account = resolve_export_binding(trigger, credentials)
    return TriggerDocument.model_validate(
        {
            "name": trigger.name,
            "project": project,
            "version": trigger.version,
            "enabled": trigger.enabled,
            "machine": trigger.machine,
            "sfapi_username": username,
            "sfapi_account": account,
            "pipeline": canonical_pipeline(trigger.pipeline, response=True),
        }
    )


def dump_editable_yaml(document: TriggerDocument) -> str:
    """Emit exactly one editable document, with no managed fields or aliases."""
    body = document.model_dump(mode="json", exclude_none=True)
    return yaml.dump(
        body,
        Dumper=_EditableYAML,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=100,
    )


def exact_name_candidates(client: EBSClient, name: str) -> list[Trigger]:
    """One bounded apply lookup; never overlook duplicates beyond that page."""
    page = client.list_triggers(name=name, limit=NAME_LOOKUP_LIMIT, offset=0)
    if page.total > len(page.items):
        raise CLIError(
            "Too many triggers resemble the requested name to inspect in one bounded page. "
            "Operation cancelled without guessing an identity.",
            exit_code=2,
        )
    return [trigger for trigger in page.items if trigger.name == name]
