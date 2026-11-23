# Triggers and events

Return to the [guide index](index.md). See [Authentication and Projects](authentication.md) for tokens and Project selection.

## Triggers

List, inspect, and change trigger state:

```sh
ebs triggers list --name data          # case-insensitive substring filter
ebs triggers show data-ready
ebs triggers disable data-ready
ebs triggers enable data-ready
```

`ebs triggers list` returns one page at a time (`--limit` up to 100, default 50; use `--offset` for the next page). A trigger is referenced by its exact, case-sensitive name or its ID; if a name is shared by several triggers, the command lists their IDs so you can pick one.

## Creating and applying JSON or YAML documents

```sh
ebs triggers create --file trigger.yaml   # registers one new trigger
ebs triggers apply --file trigger.yaml    # creates it, or fully updates it
```

`triggers apply` is a **full update, not a dry run**: it creates the trigger if it is missing, or replaces an existing trigger's settings with the document's contents. An enabled trigger can submit real jobs to the scheduler. Omitted optional fields fall back to their defaults (for example `enabled: true`), so apply complete documents. `apply` never renames a trigger, moves it between Projects, or deletes triggers missing from the file.

Every document needs `name`, `project`, and `pipeline` fields, plus optional `version`, `enabled`, `machine`, `sfapi_username`, and `sfapi_account`. The `project` field must match your selected Project (`--project`, `EBS_PROJECT`, or your saved default). Documents never contain credential material — the scheduler credential is chosen by **Username** and **Account**.

### S3 start-job with an inline template

`source: template` holds the batch script inline in the document:

```yaml
outputs:
  mode: start_job
  job_spec:
    source: template
    template: |
      #!/bin/bash
      echo process {{ payload.s3.key }}
```

Inline templates using a bound credential require a valid **Account**. Templates can use `{{ payload.s3.key }}` for the object key and `{{ job.id }}` for the EBS job record's identifier (not the Slurm job ID).

For a complete, ready-to-edit document — including the inline batch script, a presigned-URL download of the matching object, and the `test -s catalog.csv` result check — see [`examples/fits-starfinder.yaml`](../examples/fits-starfinder.yaml).

### S3 start-job with a scheduler-hosted script

The pipeline can instead reference a batch file that already exists on the scheduler (Account is optional in this form):

```yaml
name: scheduler-path-example
project: example-project
version: v1
enabled: true
machine: example-scheduler
sfapi_username: example-user
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

`source: path` names a **scheduler-hosted path** — the CLI never reads or uploads a local script.

### S3 matchers: glob or regex

The simplest form is one full S3 URI, matched as a **glob**:

```yaml
pipeline:
  input_type: s3_bucket_events
  matchers: s3://example-input-bucket/????-??-??/reports.txt
```

- `s3://bucket` or `s3://bucket/` selects a whole bucket; `s3://bucket/incoming/` selects that literal prefix.
- In the pattern part, `*` and `?` are wildcards; every other character matches literally (glob syntax, not a regular expression).
- Limits: at most 12 `*`/`?` wildcards, a 256-character path, and one lowercase dot-suffix (up to 32 characters) after the last `*`, such as `.nc` or `.txt`.

For fuller patterns, the `regex:` shorthand matches keys with a raw **RE2 regular expression** instead (Go `regexp` syntax):

```yaml
matchers:
  s3_bucket: example-input-bucket
  path_regex: ^\d{4}-\d{2}-\d{2}/reports\.txt$
```

Key differences from the glob shorthand: the pattern is a raw RE2 expression over the complete object key relative to the bucket, it is **not implicitly anchored** — write `^` and `$` when you want whole-key matching — and a raw `*` repeats the preceding expression rather than meaning "anything". Keep the pattern nonblank and at most 256 characters.

`path_prefix`, `path_glob`, and `path_regex` are mutually exclusive — author one key matcher — and combine freely with other filters such as `event_type` and `file_types`. Job-event matchers must use the structured object form.

### Callback

A callback records matching events and posts them to an HTTP(S) URL, with no scheduler machine or credential binding:

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

### Standard input and export

Pipe a document in with an explicit syntax (regular files must end in `.json`, `.yaml`, or `.yml`; problems are reported before anything is created or updated):

```sh
cat trigger.yaml | ebs triggers apply --file - --format yaml
```

`triggers export` writes one editable YAML document to stdout, ready to edit and re-apply. It accepts an exact name or trigger ID:

```sh
ebs --project example-project triggers export fits-ready > trigger.yaml
# Edit trigger.yaml, including Username and Account if a credential is bound.
ebs --project example-project triggers apply --file trigger.yaml
```

## Deleting a trigger

`ebs triggers delete TRIGGER_REF` permanently deletes one trigger **and its event and job history**. `disable` is the reversible alternative: it stops the trigger but keeps it and its history.

Attached to a terminal, the command shows the trigger's name, ID, and Project and asks for confirmation; the default answer is No, so pressing Enter cancels. In scripts, pass `--yes` to skip the prompt:

```sh
ebs triggers delete data-ready
```

## Events and jobs

List the events a trigger received and the jobs they started:

```sh
ebs events list data-ready
ebs events list data-ready --status failed --source s3 --limit 50
```

`--limit` accepts 1 through 200 and defaults to 50; each call returns one bounded page. When more events are available, the output shows a cursor — pass it back exactly as returned:

```sh
ebs events list data-ready --cursor OPAQUE_CURSOR_FROM_PREVIOUS_RESPONSE
```

The table includes the associated job ID, job status, and a bounded failure reason. Event payloads and rendered job scripts are not printed.

Next: [S3 operations and boto3](s3.md).
