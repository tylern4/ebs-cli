# Triggers and events

Return to the [guide index](index.md). See [Authentication and Projects](authentication.md) for tokens and Project selection.

## Triggers

List one page, filter by name, show a trigger, or change its state:

```sh
ebs triggers list --limit 50 --offset 0
ebs triggers list --enabled
ebs triggers list --disabled
ebs triggers list --name data
ebs triggers show data-ready
ebs triggers disable data-ready
ebs triggers enable 12345678-1234-5678-1234-567812345678
```

`--limit` accepts 1 through 100 and defaults to 50. Use the reported total and `--offset` to request another page. `--name` is a case-insensitive substring filter that stays within the selected Project and one bounded page.

### Creating and applying JSON or YAML documents

`triggers create` registers exactly one new trigger. `triggers apply` uses the exact,
case-sensitive Project/name identity to create a missing trigger, replace all mutable
state on an existing trigger, or make no change when the canonical states match:

```sh
ebs triggers create --file trigger.json
ebs triggers apply --file trigger.yaml
ebs triggers apply --file trigger.yml
ebs triggers apply --file trigger.json --json
```

A document has required `name`, `project`, and `pipeline` fields. Its existing mutable
scalar fields are `version` (default `v1`), `enabled` (default `true`), `machine`
(default null), `sfapi_username`, and `sfapi_account`. `apply` is a **full-state** operation,
not a partial patch: omitted optional scalars receive those defaults, both credential
fields are cleared when no Username is authored, and `pipeline` is replaced wholesale.
It never renames or moves a trigger between Projects and never deletes triggers that are
absent from a file. Unknown or response-managed fields, including `sfapi_token_ref`,
`id`, timestamps, owner information, and `job_spec.machine`, are rejected.

A requested document Project must exactly match an effective `--project`,
`EBS_PROJECT`, or saved default selection when one exists. With no existing selection,
the document supplies the Project. The selected name must match exactly one available
Project. Successful create/apply output reports only `created`, `updated`, or already up
to date, the exact name, Project, and trigger ID; `--json` exposes those same stable
fields without the pipeline or request body.

When an existing trigger is unchanged, `apply` performs its parsing and exact identity,
Project, credential-metadata, and canonical-state checks but sends no create or PATCH
request. This is the non-mutating check for an existing document. It is not an offline
linter: an absent trigger is created, and the server remains authoritative for scheduler,
matcher, template-render, and credential eligibility validation.

#### S3 start-job with an inline template

This synthetic JSON example uses an inline script template and selects the scheduler
credential by **Username** and **Account**, rather than an internal credential UUID:

```json
{
  "name": "fits-ready",
  "project": "example-project",
  "version": "v1",
  "enabled": true,
  "machine": "example-scheduler",
  "sfapi_username": "example-user",
  "sfapi_account": "example-account",
  "pipeline": {
    "input_type": "s3_bucket_events",
    "matchers": {
      "event_type": "s3.object.created",
      "s3_bucket": "example-input-bucket",
      "path_prefix": "incoming/"
    },
    "outputs": {
      "mode": "start_job",
      "job_spec": {
        "source": "template",
        "template": "#!/bin/bash\necho process {{ payload.s3.key }}\n"
      }
    }
  }
}
```

`source: template` carries that inline script. Inline-template jobs using a bound
credential require a valid Account under the existing backend contract.

When the object pattern is the only S3 filter, `matchers` can instead be one full S3 URI:

```yaml
pipeline:
  input_type: s3_bucket_events
  matchers: s3://cfs/????-??-??/*.list.allfiles
```

The CLI translates that example before any API request to the existing structured fields:

```yaml
matchers:
  s3_bucket: cfs
  path_glob: ????-??-??/*
  file_types: [.list.allfiles]
```

`s3://bucket` and `s3://bucket/` select a whole bucket. `s3://bucket/incoming/` selects
that literal prefix. The part after the final `*` can be one lowercase dot-suffix, such as
`.nc` or `.list.allfiles`, of at most 32 characters. Patterns allow at most 12 `*` or `?`
wildcards and a 256-character path. This is glob syntax, not raw regular expression
syntax: `*` and `?` are wildcards, while all other regex metacharacters match literally.

For fuller patterns, the explicit `regex:` shorthand selects keys with a raw RE2 regular
expression instead of a glob:

```yaml
pipeline:
  input_type: s3_bucket_events
  matchers: regex:s3://cfs/^\d{4}-\d{2}-\d{2}/.*\.list\.allfiles$
```

The CLI translates that scalar before any API request to `s3_bucket: cfs` plus a
`path_regex` field carrying the raw pattern exactly as written. The same matcher in the
structured form:

```yaml
matchers:
  s3_bucket: cfs
  path_regex: ^\d{4}-\d{2}-\d{2}/.*\.list\.allfiles$
```

The value is a raw RE2 regular expression (the syntax implemented by Go `regexp`) over
the complete object key relative to the bucket, with ordinary search semantics: it is not
implicitly anchored to the whole key, so author `^` and `$` when whole-key matching is
wanted. Backreferences, look-around, and the C++-only `\C` byte escape are invalid. A
`\Q...\E` literal quote must include its closing `\E`. Noncapturing groups `(?:…)`,
counted repetitions `{2,4}`, alternation `|`, and inline flags such as `(?i)` work. A raw `*`
repeats the preceding expression; it does not have the glob's standalone-anything
meaning. The pattern must be nonblank and at most 256 characters, the `regex:` prefix is
lowercase, the bucket must be nonblank, and the raw pattern is carried verbatim — literal
spaces inside it are kept. The glob shorthand is unchanged: `s3://bucket/<pattern>`
remains a bounded glob where `*` and `?` are wildcards and every other character matches
literally.

`path_prefix`, `path_glob`, and `path_regex` are mutually exclusive — author one key
matcher — and combine freely with the other filters, including `s3_events` and
`file_types`. The example above matches
`2026-11-04/mmapplypolicy.1539061.81DE84C7.list.allfiles` and does not match a
date-shaped key whose year contains letters.

Use a structured matchers object to combine a pattern with S3 event types or any other
filter. Job-event matchers must also be structured objects. Export emits the canonical
structured object, including a stored `path_regex` unchanged, so an exported trigger can
be applied without an update.

#### S3 start-job with a scheduler-hosted script

The same pipeline can instead reference an existing batch file on the scheduler:

```yaml
name: scheduler-path-example
project: example-project
version: v1
enabled: true
machine: example-scheduler
sfapi_username: example-user
# Account is optional for source: path under the existing backend contract.
pipeline:
  input_type: s3_bucket_events
  matchers:
    event_type: s3.object.created
    s3_bucket: example-input-bucket
    path_prefix: incoming/
  outputs:
    mode: start_job
    job_spec:
      source: path
      path: /example/scheduler/jobs/process.sh
```

`source: path` is a **scheduler-hosted path**, not a local file upload. The CLI does not
read or upload that script. Documents never contain credential material, saved Globus
grants, presigned URLs, or credential/configuration paths.

#### Callback

A callback records matching events and posts them to an HTTP(S) URL. It has no scheduler
machine or scheduler credential binding:

```yaml
name: completed-callback
project: example-project
version: v1
enabled: true
pipeline:
  input_type: job_events
  matchers:
    job_states: [COMPLETED]
  outputs:
    mode: callback
    url: https://hooks.example.test/events
```

#### Standard input and strict parsing

Use `--file -` with an explicit syntax; extension-based detection is unavailable on
standard input:

```sh
cat trigger.yaml | ebs triggers apply --file - --format yaml
cat trigger.json | ebs triggers create --file - --format json
```

Files must end in `.json`, `.yaml`, or `.yml`. Input is limited to 1 MiB of UTF-8 text,
one root object and one YAML document. JSON and YAML duplicate keys are errors. Unsafe
YAML tags, aliases, and merge shortcuts are rejected. Missing, unreadable, oversized,
wrong-extension, malformed, and document-validation failures occur before any trigger
write. Error messages identify document-field paths without printing local file paths,
complete templates, document contents, or request bodies.

### Export, edit, and apply

`triggers export` writes one editable YAML document to stdout and nothing else:

```sh
ebs --project example-project triggers export fits-ready > trigger.yaml
# Edit trigger.yaml, including Username and Account if a credential is bound.
ebs --project example-project triggers apply --file trigger.yaml
```

The export accepts an exact name or trigger UUID in the selected Project. It includes
only authoring fields and omits IDs, timestamps, owners, instance state, internal Project
identifiers, credential UUIDs, and derived `job_spec.machine`. A stored credential UUID
is reverse-mapped through accessible non-secret metadata to a Username; a stored Account
is checked against that same credential's Accounts. Missing, inaccessible, ambiguous, or
inconsistent metadata fails without emitting a misleading document. UUID exports also
verify the trigger's presence in the selected Project rather than guessing its name.
Redirection therefore preserves clean YAML suitable for the subsequent `apply`.

### Trigger references

Every command that names a trigger, including `triggers show`, `enable`, `disable`, and `delete`, `events list`, and every `s3` command, accepts a `TRIGGER_REF`: either the trigger's ID (UUID) or its exact, case-sensitive name in the selected or default Project:

```sh
ebs triggers show data-ready
ebs events list data-ready
ebs s3 ls data-ready --prefix run/
```

A syntactically valid UUID is used directly, with no name lookup and unchanged behavior. Any other reference is resolved with one bounded, Project-scoped listing filtered by that name, and must match exactly: no partial or case-insensitive match is ever chosen silently. Zero matches fail with guidance to run `ebs triggers list --name REF`; a name shared by several triggers fails closed and lists their trigger IDs so you can pick one; when too many triggers resemble the name to inspect in one bounded page, use the trigger ID instead. Resolution never contacts another Project and never displays credentials or signed URLs. `--json` output is identical for both reference forms.

### Deleting a trigger

`ebs triggers delete TRIGGER_REF` permanently deletes one trigger by ID or exact name. Deleting is an explicit alternative to disabling: `disable` stops the service and keeps the trigger and its history, while `delete` removes the trigger and also deletes its event and job history. You do not need to disable a trigger first, and disabling never deletes anything.

Before sending the delete request, the CLI resolves the reference within the selected Project and shows the trigger's name, ID, and Project, then asks for confirmation. The default answer is No, so pressing Enter cancels. Declining or ending the input sends nothing to the server. In a noninteractive session you must pass `--yes`; it skips the prompt but still performs the same Project-scoped resolution, and the server's authorization checks always apply:

```sh
ebs triggers delete data-ready
ebs triggers delete 12345678-1234-5678-1234-567812345678 --yes
```

As with other trigger commands, `--json` writes stable JSON describing the deleted trigger.

## Events and jobs

The trigger can be a UUID or an exact name (see [Trigger references](#trigger-references)):

```sh
ebs events list 12345678-1234-5678-1234-567812345678
ebs events list 12345678-1234-5678-1234-567812345678 \
  --status failed --source s3 --limit 50
```

`--limit` accepts 1 through 200 and defaults to 50. Each call fetches one bounded page of at most 200 events. When the response has another page, use its cursor exactly as returned:

```sh
ebs events list 12345678-1234-5678-1234-567812345678 \
  --cursor OPAQUE_CURSOR_FROM_PREVIOUS_RESPONSE
```

The table includes the associated job ID, job status, and a bounded failure reason. Event payloads and rendered job scripts are not printed. Failure text is treated as plain text, not terminal markup.

Next: [S3 operations and boto3](s3.md).
