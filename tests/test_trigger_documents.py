import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import TRIGGER_ID, trigger_data
from typer.testing import CliRunner

from ebs_cli import cli
from ebs_cli.cli import app
from ebs_cli.config import save_project
from ebs_cli.errors import CLIError
from ebs_cli.models import OffsetPage, Project, SfapiTokenMetadata, Trigger
from ebs_cli.trigger_documents import (
    MAX_DOCUMENT_BYTES,
    canonical_pipeline,
    canonical_scheduler_account,
    parse_trigger_document,
)

OTHER_ID = "99999999-8888-7777-6666-555555555555"
TOKEN_REF = "00000000-0000-7000-8000-00000000c001"
OTHER_TOKEN_REF = "00000000-0000-7000-8000-00000000c002"


def document(*, callback: bool = False, source: str = "template", **changes: Any) -> dict[str, Any]:
    if callback:
        pipeline = {
            "input_type": "job_events",
            "matchers": {"job_states": ["COMPLETED"]},
            "outputs": {"mode": "callback", "url": "https://hooks.example.test/events"},
        }
    else:
        spec = (
            {"source": "path", "path": "/scheduler/jobs/run.sh"}
            if source == "path"
            else {"source": "template", "template": "#!/bin/bash\necho example\n"}
        )
        pipeline = {
            "input_type": "s3_bucket_events",
            "matchers": {"event_type": "s3.object.created", "s3_bucket": "example-bucket"},
            "outputs": {"mode": "start_job", "job_spec": spec},
        }
    body: dict[str, Any] = {
        "name": "data-ready",
        "project": "physics",
        "version": "v3",
        "enabled": True,
        "pipeline": pipeline,
    }
    if not callback:
        body["machine"] = "perlmutter"
    body.update(changes)
    return body


def token_metadata(*, username: str = "alice", token_ref: str = TOKEN_REF, accounts: list[str] | None = None):
    return SfapiTokenMetadata.model_validate(
        {
            "token_ref": token_ref,
            "sfapi_username": username,
            "uploader": "owner",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "issued_at": None,
            "expires_at": "2037-01-01T00:00:00Z",
            "accounts": accounts if accounts is not None else ["alice", "c_shared"],
            "can_manage": True,
        }
    )


def response_for(payload: dict[str, Any], identifier: str = str(TRIGGER_ID)) -> Trigger:
    pipeline = copy.deepcopy(payload["pipeline"])
    outputs = pipeline["outputs"]
    if outputs["mode"] == "start_job":
        outputs["job_spec"]["machine"] = payload["machine"]
    return Trigger.model_validate(
        trigger_data(
            id=identifier,
            name=payload["name"],
            version=payload["version"],
            group_name=None,
            pipeline=pipeline,
            enabled=payload["enabled"],
            machine=payload.get("machine"),
            sfapi_token_ref=payload["sfapi_token_ref"],
            sfapi_account=payload["sfapi_account"],
        )
    )


class DocumentAPI:
    def __init__(self) -> None:
        self.project: str | None = None
        self.calls: list[tuple[Any, ...]] = []
        self.projects = [Project(name="physics", role="maintainer")]
        self.credentials = [token_metadata()]
        self.current: Trigger | None = None
        self.lookup_page: OffsetPage[Trigger] | None = None
        self.export_trigger: Trigger | None = None
        self.write_error: CLIError | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def list_projects(self):
        self.calls.append(("list_projects",))
        return self.projects

    def list_sfapi_tokens(self):
        self.calls.append(("list_sfapi_tokens",))
        return self.credentials

    def list_triggers(self, **kwargs: Any):
        self.calls.append(("list_triggers", kwargs))
        if self.lookup_page is not None:
            return self.lookup_page
        items = [self.current] if self.current is not None and self.current.name == kwargs.get("name") else []
        return OffsetPage(items=items, total=len(items), limit=kwargs["limit"], offset=0)

    def create_trigger(self, payload: dict[str, Any]) -> Trigger:
        self.calls.append(("create_trigger", copy.deepcopy(payload)))
        if self.write_error is not None:
            raise self.write_error
        self.current = response_for(payload)
        return self.current

    def update_trigger(self, trigger_id: Any, payload: dict[str, Any]) -> Trigger:
        self.calls.append(("update_trigger", trigger_id, copy.deepcopy(payload)))
        if self.write_error is not None:
            raise self.write_error
        self.current = response_for(payload, str(trigger_id))
        return self.current

    def get_trigger(self, trigger_id: Any) -> Trigger:
        self.calls.append(("get_trigger", trigger_id))
        assert self.export_trigger is not None
        return self.export_trigger


@pytest.fixture
def document_api(monkeypatch: pytest.MonkeyPatch) -> DocumentAPI:
    fake = DocumentAPI()

    def normal_client(_options: Any, project: str | None):
        fake.project = project
        return fake

    monkeypatch.setattr(cli, "_normal_api_client", normal_client)
    monkeypatch.setattr(cli, "api_client", lambda _ctx: fake)
    return fake


def write_document(path: Path, body: dict[str, Any], syntax: str | None = None) -> Path:
    syntax = syntax or ("json" if path.suffix == ".json" else "yaml")
    content = json.dumps(body) if syntax == "json" else yaml.safe_dump(body, sort_keys=False)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("suffix", [".json", ".yaml", ".yml"])
def test_create_accepts_each_extension_and_preserves_inline_template(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, suffix: str
) -> None:
    path = write_document(tmp_path / f"trigger{suffix}", document())

    result = runner.invoke(app, ["triggers", "create", "--file", str(path), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "action": "created",
        "id": str(TRIGGER_ID),
        "name": "data-ready",
        "project": "physics",
    }
    method, payload = document_api.calls[-1]
    assert method == "create_trigger"
    assert payload["group"] == "physics"
    assert payload["pipeline"]["outputs"]["job_spec"] == {
        "source": "template",
        "template": "#!/bin/bash\necho example\n",
    }
    assert payload["sfapi_token_ref"] is None
    assert payload["sfapi_account"] is None
    assert [call[0] for call in document_api.calls] == ["list_projects", "create_trigger"]
    assert "group" not in json.loads(result.stdout)


def test_create_path_is_scheduler_reference_not_a_local_upload(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    path = write_document(tmp_path / "trigger.yaml", document(source="path"))

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 0, result.output
    payload = document_api.calls[-1][1]
    assert payload["pipeline"]["outputs"]["job_spec"] == {
        "source": "path",
        "path": "/scheduler/jobs/run.sh",
    }
    assert "created:" in result.stdout
    assert "#!/bin/bash" not in result.output
    assert str(path) not in result.output


@pytest.mark.parametrize("syntax", ["json", "yaml"])
def test_stdin_requires_and_uses_explicit_format(
    runner: CliRunner, document_api: DocumentAPI, syntax: str
) -> None:
    content = json.dumps(document()) if syntax == "json" else yaml.safe_dump(document())

    result = runner.invoke(app, ["triggers", "create", "--file", "-", "--format", syntax], input=content)
    missing = runner.invoke(app, ["triggers", "create", "--file", "-"], input=content)

    assert result.exit_code == 0, result.output
    assert missing.exit_code == 2
    assert "--format" in missing.stderr
    assert len([call for call in document_api.calls if call[0] == "create_trigger"]) == 1


@pytest.mark.parametrize("syntax", ["json", "yaml"])
@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("s3://imaging-lab", {"s3_bucket": "imaging-lab"}),
        ("s3://imaging-lab/", {"s3_bucket": "imaging-lab"}),
        ("s3://b/archive/", {"s3_bucket": "b", "path_prefix": "archive/"}),
        ("s3://b/archive/*", {"s3_bucket": "b", "path_prefix": "archive/"}),
        (
            "s3://imaging-lab/frames/*.jpeg",
            {"s3_bucket": "imaging-lab", "path_prefix": "frames/", "file_types": [".jpeg"]},
        ),
        (
            "s3://cfs/????-??-??/*.list.allfiles",
            {"s3_bucket": "cfs", "path_glob": "????-??-??/*", "file_types": [".list.allfiles"]},
        ),
        (
            "s3://b/runs/202?/*.nc",
            {"s3_bucket": "b", "path_glob": "runs/202?/*", "file_types": [".nc"]},
        ),
        ("s3://b/runs/202?/*", {"s3_bucket": "b", "path_glob": "runs/202?/*"}),
        (
            "s3://b/[ab]+.(x)/sample_?.csv",
            {"s3_bucket": "b", "path_glob": "[ab]+.(x)/sample_?.csv"},
        ),
        (
            "s3://b/a*b*.jpeg",
            {"s3_bucket": "b", "path_glob": "a*b*", "file_types": [".jpeg"]},
        ),
    ],
)
def test_s3_uri_scalar_parses_to_canonical_structured_matchers(
    syntax: str, pattern: str, expected: dict[str, Any]
) -> None:
    body = document()
    body["pipeline"]["matchers"] = pattern
    content = json.dumps(body) if syntax == "json" else yaml.safe_dump(body)

    parsed = parse_trigger_document(content.encode(), syntax)

    assert parsed.pipeline.matchers.model_dump(exclude_none=True) == expected
    assert canonical_pipeline(parsed.pipeline)["matchers"] == expected


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("s3://b/" + "?" * 11 + "/*", "?" * 11 + "/*"),
        ("s3://b/" + "a" * 254 + "?*", "a" * 254 + "?*"),
    ],
)
def test_s3_uri_accepts_wildcard_and_path_boundaries(pattern: str, expected: str) -> None:
    body = document()
    body["pipeline"]["matchers"] = pattern

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")

    assert parsed.pipeline.matchers.path_glob == expected


def test_s3_uri_accepts_32_character_canonical_suffix() -> None:
    suffix = ".a" * 16
    body = document()
    body["pipeline"]["matchers"] = f"s3://b/*{suffix}"

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")

    assert parsed.pipeline.matchers.file_types == [suffix]


def test_existing_structured_s3_matchers_are_unchanged() -> None:
    matchers = {
        "event_type": "s3.object.created",
        "s3_events": ["s3.object.created"],
        "s3_bucket": "b",
        "path_glob": "[ab]+.(x)/sample_?.csv",
        "file_types": [".csv"],
        "user": "example",
        "group": "example-project",
    }
    body = document()
    body["pipeline"]["matchers"] = copy.deepcopy(matchers)

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")

    assert canonical_pipeline(parsed.pipeline)["matchers"] == matchers


# The owner's raw-RE2 example: a date-directory prefix with a literal-dot
# suffix, matched with ordinary RE2 search semantics.
OWNER_PATH_REGEX = r"^\d{4}-\d{2}-\d{2}/.*\.list\.allfiles$"
OWNER_REGEX_SCALAR = f"regex:s3://cfs/{OWNER_PATH_REGEX}"
OWNER_REGEX_MATCHERS = {"s3_bucket": "cfs", "path_regex": OWNER_PATH_REGEX}


@pytest.mark.parametrize("syntax", ["json", "yaml"])
def test_regex_scalar_owner_example_parses_verbatim(syntax: str) -> None:
    body = document()
    body["pipeline"]["matchers"] = OWNER_REGEX_SCALAR
    content = json.dumps(body) if syntax == "json" else yaml.safe_dump(body)

    parsed = parse_trigger_document(content.encode(), syntax)

    assert parsed.pipeline.matchers.model_dump(exclude_none=True) == OWNER_REGEX_MATCHERS
    assert canonical_pipeline(parsed.pipeline)["matchers"] == OWNER_REGEX_MATCHERS
    # The raw leaf reaches the canonical state byte-identical: no glob
    # decomposition, prefix conversion, or file-type inference ran.
    assert parsed.pipeline.matchers.file_types is None
    assert parsed.pipeline.matchers.path_glob is None
    assert parsed.pipeline.matchers.path_prefix is None


@pytest.mark.parametrize(
    "raw",
    [
        r"^run_\d+\.log(\.gz)?$",
        r"^(a|b){2,4}/part-(?:one|two)\.dat$",
        r"(?i)^nightly-build.*\.tar\.bz2$",
        "????-??-??/*",
        r"^spaces  inside  key$",
    ],
)
def test_regex_scalar_keeps_raw_syntax_slashes_and_spaces(raw: str) -> None:
    body = document()
    body["pipeline"]["matchers"] = f"regex:s3://b/{raw}"

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")

    assert parsed.pipeline.matchers.model_dump(exclude_none=True) == {
        "s3_bucket": "b",
        "path_regex": raw,
    }


def test_regex_scalar_keeps_outer_trim_only_and_inner_spaces() -> None:
    body = document()
    # Only the scalar's existing outer whitespace trim runs: the space
    # between the bucket slash and the pattern is part of the raw value.
    body["pipeline"]["matchers"] = "  regex:s3://b/ ^anchored$  "

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")

    assert parsed.pipeline.matchers.model_dump(exclude_none=True) == {
        "s3_bucket": "b",
        "path_regex": " ^anchored$",
    }


def test_regex_scalar_applies_no_glob_caps_or_file_type_inference() -> None:
    body = document()
    # 13 '?' wildcards (over the glob cap) plus a '.jpeg'-like tail and a
    # '*.tmp' middle: all pass through as raw RE2 with no translation.
    raw = "?" * 13 + "/*mid*.jpeg"
    body["pipeline"]["matchers"] = f"regex:s3://private-bucket/{raw}"

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")

    assert parsed.pipeline.matchers.model_dump(exclude_none=True) == {
        "s3_bucket": "private-bucket",
        "path_regex": raw,
    }
    assert parsed.pipeline.matchers.file_types is None
    assert parsed.pipeline.matchers.path_glob is None


def test_structured_path_regex_combines_with_other_filters() -> None:
    matchers = {
        "event_type": "s3.object.created",
        "s3_events": ["s3.object.created", "s3.object.deleted"],
        "s3_bucket": "cfs",
        "path_regex": OWNER_PATH_REGEX,
        "file_types": [".list.allfiles"],
        "user": "example",
    }
    body = document()
    body["pipeline"]["matchers"] = copy.deepcopy(matchers)

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")

    assert canonical_pipeline(parsed.pipeline)["matchers"] == matchers


def test_regex_scalar_create_and_apply_send_only_canonical_matchers(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    body = document()
    body["pipeline"]["matchers"] = OWNER_REGEX_SCALAR
    path = write_document(tmp_path / "trigger.json", body)

    created = runner.invoke(app, ["triggers", "create", "--file", str(path), "--json"])

    assert created.exit_code == 0, created.output
    payload = document_api.calls[-1][1]
    assert payload["pipeline"]["matchers"] == OWNER_REGEX_MATCHERS


def test_regex_scalar_matches_existing_structured_state_without_patch(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    stored = document()
    stored["pipeline"]["matchers"] = dict(OWNER_REGEX_MATCHERS)
    document_api.current = response_for({**stored, "sfapi_token_ref": None, "sfapi_account": None})
    authored = document()
    authored["pipeline"]["matchers"] = OWNER_REGEX_SCALAR
    path = write_document(tmp_path / "trigger.yaml", authored)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "unchanged"
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


def test_regex_structured_export_then_apply_is_noop(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    stored = document()
    structured = {
        "event_type": "s3.object.created",
        "s3_bucket": "cfs",
        "path_regex": OWNER_PATH_REGEX,
    }
    stored["pipeline"]["matchers"] = structured
    set_export(document_api, stored)
    exported = runner.invoke(app, ["triggers", "export", "data-ready"])
    assert exported.exit_code == 0, exported.output
    assert yaml.safe_load(exported.stdout)["pipeline"]["matchers"] == structured
    path = tmp_path / "regex-roundtrip.yaml"
    path.write_text(exported.stdout, encoding="utf-8")
    document_api.current = document_api.export_trigger
    document_api.export_trigger = None

    applied = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["action"] == "unchanged"
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


@pytest.mark.parametrize(
    "pattern,secret",
    [
        ("regex:", None),
        ("regex", None),
        ("Regex:s3://b/^a$", "^a$"),
        ("regex:s3://", None),
        ("regex:s3://private-bucket", "private-bucket"),
        ("regex:s3:///runs/ebs_private_marker", "ebs_private_marker"),
        ("regex:s3://   /runs/ebs_private_marker", "ebs_private_marker"),
        ("regex:s3://private-bucket/", "private-bucket"),
        ("regex:s3://private-bucket/   ", "private-bucket"),
        ("regex:s3://private-bucket/^" + "a" * 257 + "$", "a" * 257),
    ],
)
def test_invalid_regex_scalar_is_private_and_fails_before_client_construction(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pattern: str,
    secret: str | None,
) -> None:
    body = document()
    body["pipeline"]["matchers"] = pattern
    path = write_document(tmp_path / "trigger.json", body)

    def should_not_construct(*args: Any):
        raise AssertionError("client constructed for invalid S3 regex matcher")

    monkeypatch.setattr(cli, "_normal_api_client", should_not_construct)
    result = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert result.exit_code == 2
    assert "pipeline.matchers" in result.stderr
    output = result.stdout + result.stderr
    if secret is not None:
        assert secret not in output
    if pattern.strip() not in {"regex", "regex:"}:
        assert pattern not in output
    assert "ebs_private_marker" not in output
    assert "a" * 257 not in output


def test_regex_scalar_is_forbidden_for_job_events(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = document(callback=True)
    body["pipeline"]["matchers"] = OWNER_REGEX_SCALAR
    path = write_document(tmp_path / "trigger.json", body)

    def should_not_construct(*args: Any):
        raise AssertionError("client constructed for job regex matcher scalar")

    monkeypatch.setattr(cli, "_normal_api_client", should_not_construct)
    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 2
    assert "pipeline.matchers" in result.stderr
    assert "job-event matchers must be a structured object" in result.stderr
    assert OWNER_REGEX_SCALAR not in result.stdout + result.stderr


@pytest.mark.parametrize("command", ["create", "apply"])
@pytest.mark.parametrize("syntax", ["json", "yaml"])
def test_s3_uri_create_and_apply_send_only_canonical_matchers(
    runner: CliRunner,
    document_api: DocumentAPI,
    tmp_path: Path,
    command: str,
    syntax: str,
) -> None:
    body = document()
    body["pipeline"]["matchers"] = "s3://cfs/????-??-??/*.list.allfiles"
    path = write_document(tmp_path / f"trigger.{syntax}", body, syntax)

    result = runner.invoke(app, ["triggers", command, "--file", str(path), "--json"])

    assert result.exit_code == 0, result.output
    payload = document_api.calls[-1][1]
    assert payload["pipeline"]["matchers"] == {
        "s3_bucket": "cfs",
        "path_glob": "????-??-??/*",
        "file_types": [".list.allfiles"],
    }


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "   ",
        "bucket/without/scheme/*.nc",
        "s3://",
        "s3:///prefix/*.nc",
        "s3://   /prefix/*.nc",
        "s3://private-bucket/runs/*ebs_private_suffix",
        "s3://private-bucket/runs/*.JPEG",
        "s3://private-bucket/runs/*.bad..suffix",
        "s3://private-bucket/runs/*" + ".a" * 16 + "b",
        "s3://private-bucket/" + "?" * 13,
        "s3://private-bucket/" + "a" * 257,
        "s3://private-bucket/" + "a" * 255 + "?*.nc",
    ],
)
def test_invalid_s3_uri_is_private_and_fails_before_client_construction(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pattern: str
) -> None:
    body = document()
    body["pipeline"]["matchers"] = pattern
    path = write_document(tmp_path / "trigger.yaml", body)

    def should_not_construct(*args: Any):
        raise AssertionError("client constructed for invalid S3 matcher")

    monkeypatch.setattr(cli, "_normal_api_client", should_not_construct)
    result = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert result.exit_code == 2
    assert "pipeline.matchers" in result.stderr
    output = result.stdout + result.stderr
    if pattern.strip() and pattern != "s3://":
        assert pattern not in output
    assert "private-bucket" not in output
    assert "ebs_private_suffix" not in output


def test_job_event_matcher_scalar_fails_before_client_construction(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = document(callback=True)
    body["pipeline"]["matchers"] = "s3://private-bucket/jobs/*"
    path = write_document(tmp_path / "trigger.json", body)

    def should_not_construct(*args: Any):
        raise AssertionError("client constructed for job matcher scalar")

    monkeypatch.setattr(cli, "_normal_api_client", should_not_construct)
    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 2
    assert "pipeline.matchers" in result.stderr
    assert "job-event matchers must be a structured object" in result.stderr
    assert body["pipeline"]["matchers"] not in result.stdout + result.stderr


@pytest.mark.parametrize("bad", [[], None, "text", 1])
def test_non_object_roots_are_rejected(bad: Any) -> None:
    with pytest.raises(CLIError, match="root must be one object"):
        parse_trigger_document(json.dumps(bad).encode(), "json")


@pytest.mark.parametrize(
    "content,syntax,message",
    [
        ('{"name":"one","name":"two"}', "json", "duplicate"),
        ("name: one\nname: two\n", "yaml", "duplicate"),
        ("name: one\n---\nname: two\n", "yaml", "exactly one"),
        ("defaults: &defaults\n  enabled: true\n<<: *defaults\n", "yaml", "aliases|merge"),
        ("!!python/object/apply:os.system [echo unsafe]", "yaml", "unsafe|Invalid"),
        ("{not valid", "json", "syntax"),
        ("name: [unterminated", "yaml", "unsafe|Invalid"),
    ],
)
def test_unsafe_duplicate_and_wrong_syntax_documents_fail(content: str, syntax: str, message: str) -> None:
    with pytest.raises(CLIError, match=message):
        parse_trigger_document(content.encode(), syntax)


@pytest.mark.parametrize(
    "changes,path",
    [
        ({"sfapi_token_ref": TOKEN_REF}, "sfapi_token_ref"),
        ({"id": str(TRIGGER_ID)}, "id"),
        ({"created_at": "2026-01-01T00:00:00Z"}, "created_at"),
        ({"group_id": str(TRIGGER_ID)}, "group_id"),
        ({"unexpected": True}, "unexpected"),
    ],
)
def test_unknown_and_read_only_fields_have_safe_paths(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, changes: dict[str, Any], path: str
) -> None:
    source = write_document(tmp_path / "trigger.json", document(**changes))

    result = runner.invoke(app, ["triggers", "apply", "--file", str(source)])

    assert result.exit_code == 2
    assert "document: Extra inputs are not permitted" in result.stderr
    assert f".{path}" not in result.stderr
    assert str(source) not in result.stderr
    assert document_api.calls == []


@pytest.mark.parametrize(
    "unknown_key",
    [
        "ebs_private-review-credential-value",
        "/private/var/folders/review/sensitive-trigger.yaml",
        "#!/bin/bash -e; echo complete-private-template",
    ],
)
@pytest.mark.parametrize("location", ["document", "pipeline.matchers"])
def test_sensitive_unknown_keys_never_appear_in_validation_errors(
    runner: CliRunner,
    document_api: DocumentAPI,
    tmp_path: Path,
    location: str,
    unknown_key: str,
) -> None:
    body = document()
    if location == "document":
        body[unknown_key] = "hidden"
    else:
        body["pipeline"]["matchers"][unknown_key] = "hidden"
    path = write_document(tmp_path / "trigger.json", body)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert result.exit_code == 2
    assert f"{location}: Extra inputs are not permitted" in result.stderr
    assert unknown_key not in result.stdout + result.stderr
    assert document_api.calls == []


@pytest.mark.parametrize(
    "invalid_mode",
    [
        "ebs_private-review-discriminator",
        "#!/bin/bash -e; echo complete-private-template",
    ],
)
def test_invalid_output_mode_uses_fixed_message_without_authored_tag(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, invalid_mode: str
) -> None:
    body = document()
    body["pipeline"]["outputs"]["mode"] = invalid_mode
    path = write_document(tmp_path / "trigger.json", body)

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 2
    assert "pipeline.outputs: mode must be one of: start_job, callback" in result.stderr
    assert invalid_mode not in result.stdout + result.stderr
    assert document_api.calls == []


@pytest.mark.parametrize("derived", ["perlmutter", None])
def test_derived_job_spec_machine_is_rejected_before_write(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, derived: str | None
) -> None:
    body = document()
    body["pipeline"]["outputs"]["job_spec"]["machine"] = derived
    path = write_document(tmp_path / "trigger.yaml", body)

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 2
    assert "pipeline.outputs.start_job.job_spec: Extra inputs are not permitted" in result.stderr
    assert document_api.calls == []


@pytest.mark.parametrize(
    "spec",
    [
        {"source": "path", "path": "/jobs/run.sh", "template": "echo no"},
        {"source": "template", "template": "echo yes", "path": "/jobs/run.sh"},
    ],
)
def test_nonblank_conflicting_job_spec_fails_for_the_intended_reason_before_write(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, spec: dict[str, str]
) -> None:
    body = document()
    body["pipeline"]["outputs"]["job_spec"] = spec
    path = write_document(tmp_path / "trigger.yaml", body)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert result.exit_code == 2
    assert "job_spec carries both path and template" in result.stderr
    assert document_api.calls == []


@pytest.mark.parametrize(
    "spec",
    [
        {"source": "path", "template": "echo no"},
        {"source": "path"},
        {"source": "path", "path": " \t\n"},
        {"source": "template"},
        {"source": "template", "template": " \t\n"},
        {"source": "template", "path": "/jobs/run.sh"},
    ],
)
def test_missing_or_blank_job_spec_content_fails_before_write(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, spec: dict[str, str]
) -> None:
    body = document()
    body["pipeline"]["outputs"]["job_spec"] = spec
    path = write_document(tmp_path / "trigger.yaml", body)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert result.exit_code == 2
    assert document_api.calls == []


@pytest.mark.parametrize(
    "spec",
    [
        {"source": "path", "path": "/jobs/run.sh", "template": "echo no"},
        {"source": "template", "template": "echo yes", "path": "/jobs/run.sh"},
    ],
)
def test_parser_directly_rejects_nonblank_conflicting_job_spec_siblings(spec: dict[str, str]) -> None:
    body = document()
    body["pipeline"]["outputs"]["job_spec"] = spec

    with pytest.raises(CLIError, match="job_spec carries both path and template"):
        parse_trigger_document(json.dumps(body).encode(), "json")


@pytest.mark.parametrize("blank", [None, "", " \t\n"])
@pytest.mark.parametrize("source", ["path", "template"])
def test_parser_tolerates_blank_irrelevant_siblings_and_canonicalizes_them_away(
    source: str, blank: str | None
) -> None:
    spec = {"source": source, "path": "/jobs/run.sh", "template": "echo yes"}
    irrelevant = "template" if source == "path" else "path"
    selected = "path" if source == "path" else "template"
    spec[irrelevant] = blank
    body = document()
    body["pipeline"]["outputs"]["job_spec"] = spec

    parsed = parse_trigger_document(json.dumps(body).encode(), "json")
    canonical = canonical_pipeline(parsed.pipeline)

    assert canonical["outputs"]["job_spec"] == {"source": source, selected: spec[selected]}


def test_missing_wrong_extension_unreadable_and_oversized_files_do_not_create_a_client(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    constructed = False

    def should_not_construct(*args: Any):
        nonlocal constructed
        constructed = True
        raise AssertionError("client constructed")

    monkeypatch.setattr(cli, "_normal_api_client", should_not_construct)
    directory = tmp_path / "input.yaml"
    directory.mkdir()
    oversized = tmp_path / "large.json"
    oversized.write_bytes(b" " * (MAX_DOCUMENT_BYTES + 1))

    cases = [
        [tmp_path / "missing.yaml"],
        [tmp_path / "trigger.txt"],
        [directory],
        [oversized],
    ]
    paths = []
    for case in cases:
        supplied = write_document(case[0], document()) if case[0].suffix == ".txt" else case[0]
        paths.append(str(supplied))
        result = runner.invoke(app, ["triggers", "create", "--file", str(supplied)])
        assert result.exit_code == 2
        assert str(supplied) not in result.stderr
        assert "ebs_unit-test-token" not in result.output

    assert not constructed


def test_named_file_format_mismatch_is_rejected_before_io(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    path = write_document(tmp_path / "trigger.json", document())
    result = runner.invoke(app, ["triggers", "create", "--file", str(path), "--format", "yaml"])
    assert result.exit_code == 2
    assert document_api.calls == []


def test_apply_creates_when_absent_and_full_state_defaults_are_explicit(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    body = document()
    del body["version"]
    del body["enabled"]
    path = write_document(tmp_path / "trigger.json", body)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert result.exit_code == 0, result.output
    method, payload = document_api.calls[-1]
    assert (method, payload["version"], payload["enabled"]) == ("create_trigger", "v1", True)
    assert set(payload) == {
        "group",
        "name",
        "version",
        "enabled",
        "machine",
        "sfapi_token_ref",
        "sfapi_account",
        "pipeline",
    }


def test_apply_update_replaces_pipeline_and_complete_scalars(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    old = response_for(
        {
            **document(machine="old-machine", sfapi_username="old"),
            "sfapi_token_ref": TOKEN_REF,
            "sfapi_account": "shared",
        }
    )
    old.pipeline["matchers"] = {"raw_yaml": "old: pipeline"}
    document_api.current = old
    body = document(callback=True, enabled=False)
    body["name"] = "data-ready"
    path = write_document(tmp_path / "trigger.yaml", body)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "updated"
    method, identifier, payload = document_api.calls[-1]
    assert method == "update_trigger"
    assert identifier == TRIGGER_ID
    assert payload["machine"] is None
    assert payload["sfapi_token_ref"] is None
    assert payload["sfapi_account"] is None
    assert payload["pipeline"]["outputs"] == body["pipeline"]["outputs"]
    assert "group" not in payload


def test_apply_derived_machine_and_absent_none_normalization_are_noop(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    body = document()
    body["pipeline"]["outputs"]["job_spec"]["path"] = ""
    current_body = document()
    document_api.current = response_for({**current_body, "sfapi_token_ref": None, "sfapi_account": None})
    path = write_document(tmp_path / "trigger.json", body)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "unchanged"
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)
    assert "already up to date" not in result.stdout  # --json remains machine-only


def test_s3_uri_scalar_matches_existing_structured_state_without_patch(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    stored = document()
    stored["pipeline"]["matchers"] = {
        "s3_bucket": "cfs",
        "path_glob": "????-??-??/*",
        "file_types": [".list.allfiles"],
    }
    document_api.current = response_for({**stored, "sfapi_token_ref": None, "sfapi_account": None})
    authored = document()
    authored["pipeline"]["matchers"] = "s3://cfs/????-??-??/*.list.allfiles"
    path = write_document(tmp_path / "trigger.json", authored)

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "unchanged"
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


def test_apply_noop_human_output_is_bounded(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    document_api.current = response_for({**document(), "sfapi_token_ref": None, "sfapi_account": None})
    path = write_document(tmp_path / "trigger.yaml", document())

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert result.exit_code == 0
    assert "already up to date" in result.stdout
    assert "#!/bin/bash" not in result.stdout


def test_result_uses_document_name_when_response_name_contains_terminal_controls(
    runner: CliRunner,
    document_api: DocumentAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = write_document(tmp_path / "trigger.json", document())
    real_create = document_api.create_trigger
    malicious_name = "attacker\x1b]0;injected-title\x07\nINJECTED-LINE"

    def create_with_malformed_name(payload: dict[str, Any]) -> Trigger:
        response = real_create(payload)
        response.name = malicious_name
        return response

    monkeypatch.setattr(document_api, "create_trigger", create_with_malformed_name)
    human = runner.invoke(app, ["triggers", "create", "--file", str(path)], env={"COLUMNS": "240"})
    stable = runner.invoke(app, ["triggers", "create", "--file", str(path), "--json"])

    assert human.exit_code == stable.exit_code == 0
    assert human.stdout.strip() == f"created: trigger 'data-ready' in Project 'physics' (ID {TRIGGER_ID})."
    assert len(human.stdout.splitlines()) == 1
    assert json.loads(stable.stdout) == {
        "action": "created",
        "id": str(TRIGGER_ID),
        "name": "data-ready",
        "project": "physics",
    }
    for output in (human.stdout + human.stderr, stable.stdout + stable.stderr):
        assert "\x1b" not in output
        assert "INJECTED-LINE" not in output
        assert malicious_name not in output


def test_apply_ambiguous_and_truncated_name_lookups_fail_closed(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    path = write_document(tmp_path / "trigger.json", document())
    first = response_for({**document(), "sfapi_token_ref": None, "sfapi_account": None})
    second = response_for({**document(), "sfapi_token_ref": None, "sfapi_account": None}, OTHER_ID)
    document_api.lookup_page = OffsetPage(items=[first, second], total=2, limit=100, offset=0)

    ambiguous = runner.invoke(app, ["triggers", "apply", "--file", str(path)])
    document_api.lookup_page = OffsetPage(items=[first], total=101, limit=100, offset=0)
    truncated = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert ambiguous.exit_code == truncated.exit_code == 2
    assert "ambiguous" in ambiguous.stderr
    assert "bounded" in truncated.stderr
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


def test_username_and_account_resolution_uses_nonsecret_metadata(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    body = document(sfapi_username="alice", sfapi_account="c_shared")
    path = write_document(tmp_path / "trigger.yaml", body)

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 0, result.output
    assert [call[0] for call in document_api.calls] == [
        "list_projects",
        "list_sfapi_tokens",
        "create_trigger",
    ]
    payload = document_api.calls[-1][1]
    assert payload["sfapi_token_ref"] == TOKEN_REF
    assert payload["sfapi_account"] == "shared"


@pytest.mark.parametrize("usernames", [[], ["alice", "alice"], ["other"]])
def test_missing_ambiguous_or_inaccessible_username_fails_without_write(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, usernames: list[str]
) -> None:
    document_api.credentials = [
        token_metadata(username=name, token_ref=TOKEN_REF if index == 0 else OTHER_TOKEN_REF)
        for index, name in enumerate(usernames)
    ]
    path = write_document(tmp_path / "trigger.json", document(sfapi_username="alice", sfapi_account="shared"))

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 2
    assert "Username" in result.stderr
    assert TOKEN_REF not in result.output
    assert not any(call[0] == "create_trigger" for call in document_api.calls)


@pytest.mark.parametrize("accounts", [[], ["other"], ["shared", "c_shared"]])
def test_absent_or_ambiguous_account_fails_without_write(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, accounts: list[str]
) -> None:
    document_api.credentials = [token_metadata(accounts=accounts)]
    path = write_document(tmp_path / "trigger.json", document(sfapi_username="alice", sfapi_account="shared"))

    result = runner.invoke(app, ["triggers", "apply", "--file", str(path)])

    assert result.exit_code == 2
    assert "Account" in result.stderr
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


def test_account_without_username_and_callback_binding_fail_before_client(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    account_only = write_document(tmp_path / "account.json", document(sfapi_account="shared"))
    callback = write_document(tmp_path / "callback.json", document(callback=True, sfapi_username="alice"))

    first = runner.invoke(app, ["triggers", "create", "--file", str(account_only)])
    second = runner.invoke(app, ["triggers", "create", "--file", str(callback)])

    assert first.exit_code == second.exit_code == 2
    assert document_api.calls == []


def test_callback_translation_has_no_machine_or_credentials(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    path = write_document(tmp_path / "callback.json", document(callback=True))

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 0, result.output
    payload = document_api.calls[-1][1]
    assert payload["machine"] is None
    assert payload["sfapi_token_ref"] is None
    assert payload["sfapi_account"] is None
    assert payload["pipeline"]["outputs"]["mode"] == "callback"


def test_document_project_must_match_flag_environment_and_saved_selection(
    runner: CliRunner, document_api: DocumentAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write_document(tmp_path / "trigger.json", document())
    flag = runner.invoke(app, ["--project", "other", "triggers", "create", "--file", str(path)])
    monkeypatch.setenv("EBS_PROJECT", "other")
    environment = runner.invoke(app, ["triggers", "create", "--file", str(path)])
    monkeypatch.delenv("EBS_PROJECT")
    save_project("other")
    saved = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert flag.exit_code == environment.exit_code == saved.exit_code == 2
    assert "document Project" in flag.stderr + environment.stderr + saved.stderr
    assert document_api.calls == []


def test_document_project_is_used_when_no_selection_exists_and_flag_matches(
    runner: CliRunner, document_api: DocumentAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("EBS_PROJECT")
    path = write_document(tmp_path / "trigger.json", document())

    no_selection = runner.invoke(app, ["triggers", "create", "--file", str(path)])
    document_api.calls.clear()
    matching = runner.invoke(app, ["--project", "physics", "triggers", "create", "--file", str(path)])

    assert no_selection.exit_code == matching.exit_code == 0
    assert document_api.project == "physics"


@pytest.mark.parametrize("names", [[], ["physics", "physics"], ["other"]])
def test_available_project_resolution_fails_zero_unknown_or_ambiguous(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, names: list[str]
) -> None:
    document_api.projects = [Project(name=name, role="user") for name in names]
    path = write_document(tmp_path / "trigger.json", document())

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 2
    assert "Project" in result.stderr
    assert not any(call[0] == "create_trigger" for call in document_api.calls)


def test_create_conflict_and_http_write_failure_do_not_leak_document_or_path(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    document_api.write_error = CLIError("Request failed with HTTP 409. No authored values were displayed.")
    secret_path = tmp_path / "private-trigger-name.json"
    path = write_document(secret_path, document())

    result = runner.invoke(app, ["triggers", "create", "--file", str(path)])

    assert result.exit_code == 1
    assert "HTTP 409" in result.stderr
    assert str(path) not in result.output
    assert "example-bucket" not in result.output
    assert "#!/bin/bash" not in result.output
    assert "ebs_unit-test-token" not in result.output


def set_export(document_api: DocumentAPI, body: dict[str, Any], **changes: Any) -> None:
    payload = {**body, "sfapi_token_ref": None, "sfapi_account": None, **changes}
    document_api.export_trigger = response_for(payload)
    document_api.current = document_api.export_trigger


def test_yaml_export_is_clean_editable_and_omits_managed_fields(
    runner: CliRunner, document_api: DocumentAPI
) -> None:
    set_export(document_api, document(), sfapi_token_ref=TOKEN_REF, sfapi_account="c_shared")

    result = runner.invoke(app, ["triggers", "export", "data-ready"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    exported = yaml.safe_load(result.stdout)
    assert exported["project"] == "physics"
    assert exported["sfapi_username"] == "alice"
    assert exported["sfapi_account"] == "shared"
    assert exported["pipeline"]["outputs"]["job_spec"] == document()["pipeline"]["outputs"]["job_spec"]
    assert "machine" not in exported["pipeline"]["outputs"]["job_spec"]
    for managed in (
        "id",
        "group_id",
        "group_name",
        "created_by",
        "created_at",
        "updated_at",
        "instance",
        "sfapi_token_ref",
    ):
        assert managed not in exported
    reparsed = parse_trigger_document(result.stdout.encode(), "yaml")
    assert reparsed.name == "data-ready"


def test_uuid_export_verifies_scoped_listing_when_get_omits_project(
    runner: CliRunner, document_api: DocumentAPI
) -> None:
    set_export(document_api, document(callback=True))
    document_api.current = document_api.export_trigger

    result = runner.invoke(app, ["triggers", "export", str(TRIGGER_ID)])

    assert result.exit_code == 0, result.output
    assert yaml.safe_load(result.stdout)["project"] == "physics"
    assert any(call[0] == "list_triggers" for call in document_api.calls)


def test_uuid_export_fails_instead_of_guessing_project(runner: CliRunner, document_api: DocumentAPI) -> None:
    set_export(document_api, document(callback=True))
    document_api.current = None

    result = runner.invoke(app, ["triggers", "export", str(TRIGGER_ID)])

    assert result.exit_code == 2
    assert "selected Project" in result.stderr
    assert result.stdout == ""


def test_export_refuses_mismatched_project_name(runner: CliRunner, document_api: DocumentAPI) -> None:
    set_export(document_api, document(callback=True))
    document_api.export_trigger.group_name = "other"

    result = runner.invoke(app, ["triggers", "export", "data-ready"])

    assert result.exit_code == 2
    assert result.stdout == ""


@pytest.mark.parametrize("credentials", [[], [token_metadata(), token_metadata(token_ref=OTHER_TOKEN_REF)]])
def test_export_reverse_binding_requires_exactly_one_accessible_match(
    runner: CliRunner, document_api: DocumentAPI, credentials: list[SfapiTokenMetadata]
) -> None:
    # Duplicate the same reference for the ambiguous case.
    if credentials:
        credentials[1].token_ref = credentials[0].token_ref
    document_api.credentials = credentials
    set_export(document_api, document(), sfapi_token_ref=TOKEN_REF, sfapi_account="shared")

    result = runner.invoke(app, ["triggers", "export", "data-ready"])

    assert result.exit_code == 1
    assert result.stdout == ""


def test_export_rechecks_stored_account(runner: CliRunner, document_api: DocumentAPI) -> None:
    document_api.credentials = [token_metadata(accounts=["other"])]
    set_export(document_api, document(), sfapi_token_ref=TOKEN_REF, sfapi_account="shared")

    result = runner.invoke(app, ["triggers", "export", "data-ready"])

    assert result.exit_code == 2
    assert "Account" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("callback", [False, True])
def test_export_then_apply_is_noop_for_start_job_and_callback(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path, callback: bool
) -> None:
    set_export(document_api, document(callback=callback))
    exported = runner.invoke(app, ["triggers", "export", "data-ready"])
    assert exported.exit_code == 0, exported.output
    path = tmp_path / "roundtrip.yaml"
    path.write_text(exported.stdout, encoding="utf-8")
    document_api.current = document_api.export_trigger
    document_api.export_trigger = None

    applied = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["action"] == "unchanged"
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


def test_s3_structured_export_then_apply_is_noop(
    runner: CliRunner, document_api: DocumentAPI, tmp_path: Path
) -> None:
    stored = document()
    structured = {
        "s3_bucket": "cfs",
        "path_glob": "????-??-??/*",
        "file_types": [".list.allfiles"],
    }
    stored["pipeline"]["matchers"] = structured
    set_export(document_api, stored)
    exported = runner.invoke(app, ["triggers", "export", "data-ready"])
    assert exported.exit_code == 0, exported.output
    assert yaml.safe_load(exported.stdout)["pipeline"]["matchers"] == structured
    path = tmp_path / "s3-roundtrip.yaml"
    path.write_text(exported.stdout, encoding="utf-8")
    document_api.current = document_api.export_trigger
    document_api.export_trigger = None

    applied = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["action"] == "unchanged"
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


def test_bound_start_job_export_then_apply_re_resolves_same_binding_without_writes(
    runner: CliRunner,
    document_api: DocumentAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document_api.credentials = [token_metadata(username="Alice", accounts=["c_shared"])]
    set_export(document_api, document(), sfapi_token_ref=TOKEN_REF, sfapi_account="shared")
    resolved: list[tuple[str | None, str | None]] = []
    real_resolver = cli.resolve_document_binding

    def record_resolution(authored: Any, credentials: list[SfapiTokenMetadata]):
        token_ref, account = real_resolver(authored, credentials)
        resolved.append((str(token_ref) if token_ref is not None else None, account))
        return token_ref, account

    monkeypatch.setattr(cli, "resolve_document_binding", record_resolution)
    exported = runner.invoke(app, ["triggers", "export", "data-ready"])
    assert exported.exit_code == 0, exported.output
    authored = yaml.safe_load(exported.stdout)
    assert authored["sfapi_username"] == "Alice"
    assert authored["sfapi_account"] == "shared"
    path = tmp_path / "bound-roundtrip.yaml"
    path.write_text(exported.stdout, encoding="utf-8")
    document_api.current = document_api.export_trigger
    document_api.export_trigger = None
    document_api.calls.clear()

    applied = runner.invoke(app, ["triggers", "apply", "--file", str(path), "--json"])

    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["action"] == "unchanged"
    assert resolved == [(TOKEN_REF, "shared")]
    assert not any(call[0] in {"create_trigger", "update_trigger"} for call in document_api.calls)


def test_account_canonicalization_collisions_fail_closed() -> None:
    assert canonical_scheduler_account(["c_shared"], "c_shared") == "shared"
    with pytest.raises(CLIError, match="unavailable or ambiguous"):
        canonical_scheduler_account(["shared", "c_shared"], "shared")


def test_yaml_export_has_no_aliases_and_preserves_multiline_template(
    runner: CliRunner, document_api: DocumentAPI
) -> None:
    set_export(document_api, document())
    result = runner.invoke(app, ["triggers", "export", "data-ready"])
    assert result.exit_code == 0
    assert "template: |\n" in result.stdout
    assert "*" not in result.stdout
