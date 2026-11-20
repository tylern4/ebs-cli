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

### Trigger references

Every command that names a trigger, including `triggers show`, `enable`, and `disable`, `events list`, and every `s3` command, accepts a `TRIGGER_REF`: either the trigger's ID (UUID) or its exact, case-sensitive name in the selected or default Project:

```sh
ebs triggers show data-ready
ebs events list data-ready
ebs s3 ls data-ready --prefix run/
```

A syntactically valid UUID is used directly, with no name lookup and unchanged behavior. Any other reference is resolved with one bounded, Project-scoped listing filtered by that name, and must match exactly: no partial or case-insensitive match is ever chosen silently. Zero matches fail with guidance to run `ebs triggers list --name REF`; a name shared by several triggers fails closed and lists their trigger IDs so you can pick one; when too many triggers resemble the name to inspect in one bounded page, use the trigger ID instead. Resolution never contacts another Project and never displays credentials or signed URLs. `--json` output is identical for both reference forms.

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
