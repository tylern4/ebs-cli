# FITS uploads and mock starfinder with the CLI

Return to the [guide index](index.md), or follow the [companion web interface walkthrough](fits-starfinder-web-ui.md).

This walkthrough creates an S3 trigger named `fits-starfinder`, uploads FITS objects below `incoming/`, and starts one Slurm batch job per accepted object. Each job downloads its triggering object through the event's presigned GET URL and runs an explicitly illustrative command:

```sh
starfinder --input input.fits --output catalog.csv --threshold 5
```

**`starfinder` is a mock application.** EBS does not provide or install it. Replace it with software and paths available on your compute system, and make sure the batch job can run that command. The CSV shown later is synthetic documentation content, not output from a run performed for this guide.

All Project, bucket, machine, Username, and Account values below are synthetic. Replace them with values available to you before running the commands. No sample input FITS file is supplied; use your own valid input.

## Prerequisites

1. Install the CLI and authenticate as described in the [quick start](../README.md#quick-start) and [authentication guide](authentication.md):

   ```sh
   ebs auth login
   ```

   On a machine without a browser, use `ebs auth login --no-browser` instead.

2. You need access to a Project, permission to manage this trigger, and a provisioned input bucket for that Project. This guide uses `example-project` and `example-input-bucket`; substitute your exact Project and bucket names.

3. In the web interface's **Settings** page, the **SFAPI clients** section must already contain an unexpired client eligible to submit jobs. Use its listed scheduler Username and one of its available Accounts in place of `example-user` and `example-account`. The YAML selects that client through non-secret metadata; never put a client secret, private key, API token, or credential reference in the document.

4. Replace `example-scheduler` with an available scheduler machine. On its compute nodes, provide the mock `starfinder` executable or your replacement, `curl`, and a valid `$SCRATCH` directory.

The commands use an explicit global `--project example-project` option so they cannot accidentally use a different saved or environment-selected Project. Check that your replacement appears in your Project list and that the explicit selection is effective:

```sh
ebs projects list
ebs --project example-project projects show
```

The explicit option takes precedence over `EBS_PROJECT` and the saved default. Alternatively, `ebs projects use PROJECT` saves a default, but do not mix that convention with the explicit commands in this walkthrough. See [Authentication and Projects](authentication.md#discover-projects-and-set-a-default).

## Save the trigger document

Save the following complete document as `fits-starfinder.yaml` after replacing every synthetic value:

```yaml
name: fits-starfinder
project: example-project
version: v1
enabled: true
machine: example-scheduler
sfapi_username: example-user
sfapi_account: example-account
pipeline:
  input_type: s3_bucket_events
  matchers:
    event_type: s3.object.created
    s3_bucket: example-input-bucket
    path_prefix: incoming/
    file_types: [.fits, .fit]
  outputs:
    mode: start_job
    job_spec:
      source: template
      template: |
        #!/bin/bash
        #SBATCH --job-name=starfinder
        #SBATCH --account={{ event.account }}
        #SBATCH --nodes=1
        #SBATCH --ntasks=1
        #SBATCH --cpus-per-task=4
        #SBATCH --time=00:10:00

        set -euo pipefail

        output_dir="$SCRATCH/starfinder/{{ job.id }}"
        mkdir -p "$output_dir"
        cd "$output_dir"

        curl --fail --location --silent --show-error \
          --output input.fits \
          '{{ s3.presigned_url }}'

        starfinder --input input.fits --output catalog.csv --threshold 5
        test -s catalog.csv
```

The matchers require **all** of these conditions: an object-creation event in the selected bucket, a key beginning with `incoming/`, and a lowercase `.fits` or `.fit` suffix. Thus `incoming/run-42.fits` and `incoming/run-43.fit` each produce one event and one batch job; objects outside that prefix or with other suffixes do not match. Each accepted upload is independent.

The `source: template` value stores this inline Jinja2 script in the trigger. For this file-event family, `event.account` supplies the selected Account, `job.id` supplies the service job ID, and `s3.presigned_url` supplies the triggering object's download URL. The script deliberately uses fixed local names, `input.fits` and `catalog.csv`; it never interpolates an object key or user-controlled filename into shell syntax.

The Slurm resources are static: one node, one task, four CPUs, and ten minutes. An S3 event does not carry resource values such as `event.nodes`, so do not use job-event fields to size this allocation.

The presigned URL is a temporary bearer credential for reading the object. It is not a public or permanent link; do not print it, copy it into logs, or share it. The event supplies an expiry as well, and a job that remains queued too long can start after its URL has expired. In that case `curl` fails and `set -e` stops the script. This example does not provide durable input staging or refresh the URL when the batch job eventually starts.

`set -euo pipefail` and the `curl` failure options stop the job after a failed download rather than treating that failure as valid FITS input. The final `test -s catalog.csv` requires a nonempty catalog, but neither that check nor a successful command exit validates the catalog's schema or scientific results. Inspect the file after completion as shown below.

If you instead choose `source: path`, its `path` must name an **already existing file on the scheduler**. Neither that form nor this inline form uploads a local script. The `source: path` contract also differs on Account selection; this walkthrough requires an Account because its inline template uses one. See [Triggers and events](triggers.md#s3-start-job-with-a-scheduler-hosted-script).

## Apply the trigger

**This is a real create or full-state update operation, not a dry run.** For the exact Project/name identity, `apply` creates a missing trigger, replaces the mutable configuration of an existing one, or reports that it is unchanged. Because this document has `enabled: true`, a successfully applied trigger can process matching uploads. The separate sample-event **Test/Verify** action in the web interface is only a simulation: it neither uploads a file nor submits a job.

Run:

```sh
ebs --project example-project triggers apply --file fits-starfinder.yaml
```

The server remains authoritative for access, matcher, template, machine, and credential eligibility checks. Confirm the trigger after applying it:

```sh
ebs --project example-project triggers show fits-starfinder
```

For apply semantics, complete-document updates, and other trigger commands, see [Creating and applying JSON or YAML documents](triggers.md#creating-and-applying-json-or-yaml-documents).

## Upload FITS objects

Put a valid local FITS file at `./run-42.fits`, then upload it to the exact matching key:

```sh
ebs --project example-project s3 upload fits-starfinder ./run-42.fits --key incoming/run-42.fits
```

To demonstrate the other accepted suffix and an independent second event/job, use another valid local file and run this separate upload:

```sh
ebs --project example-project s3 upload fits-starfinder ./run-43.fit --key incoming/run-43.fit
```

Each command uploads exactly one file directly to the trigger's bucket using temporary upload-scoped access; it neither prints those temporary credentials nor sends the object body through the EBS API. A successful upload is not a claim that its event has already been processed or that its job has finished. See [Upload a local file](s3.md#upload-a-local-file).

List a bounded page below the input prefix to verify the object keys:

```sh
ebs --project example-project s3 ls fits-starfinder --prefix incoming/ --limit 100
```

You should find `incoming/run-42.fits` and, if you ran the second upload, `incoming/run-43.fit`. Listing proves that the objects are stored, not that corresponding jobs are complete. See [List objects](s3.md#list-objects).

## Inspect events and jobs

The CLI reports each job together with its triggering event:

```sh
ebs --project example-project events list fits-starfinder --source s3 --limit 50
```

Expect one event row per accepted object. The table includes the event status, the associated **Job ID**, job status, and any recorded failure. Use the **Job ID** from the relevant event—not the separate scheduler submission ID—as the `job.id` directory component in the next section.

For stable machine-readable fields, including the job's `id` and scheduler-side `sfapi_job_id`, use:

```sh
ebs --project example-project events list fits-starfinder --json
```

Each request is one bounded page; use the returned cursor with `--cursor` if more events are available. Job submission and completion are asynchronous, so poll later if a job has not finished. The ordinary table does not print event payloads or rendered job scripts. See [Events and jobs](triggers.md#events-and-jobs).

## Inspect the source catalog on the scheduler

After the scheduler reports the job complete, inspect the output on that scheduler. Replace `SERVICE_JOB_ID` with the event row's service **Job ID**:

```sh
catalog="$SCRATCH/starfinder/SERVICE_JOB_ID/catalog.csv"
test -s "$catalog"
head -n 6 "$catalog"
```

Here `test -s` requires the catalog to exist and contain at least one byte; `head` merely displays the first lines. Neither command validates the scientific results. If the path is missing, confirm that you used the service Job ID from `events list` and inspect that event's job status and recorded failure before treating the upload as successful processing.

For this walkthrough only, a clearly synthetic mock catalog might have this content:

```csv
source_id,ra_deg,dec_deg,flux,snr
MOCK-001,150.125000,2.250000,125.0,12.5
MOCK-002,150.130000,2.255000,80.0,9.0
```

These rows are invented sample content and are **not measurements produced by a performed run**. A real catalog's columns depend on the replacement application. In this example's mock output convention, `source_id` identifies a detection, `ra_deg` and `dec_deg` are right ascension and declination in degrees, `flux` is instrumental, background-subtracted flux in application-defined units, and `snr` is the signal-to-noise ratio. Have your analysis tools parse the actual `catalog.csv`, check its header and rows against your application's schema, and evaluate detections against your scientific criteria. A completed EBS job state does not by itself prove catalog validity.
