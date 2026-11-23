# FITS uploads and mock starfinder with the CLI

Return to the [guide index](index.md), or follow the [companion web interface walkthrough](fits-starfinder-web-ui.md).

This walkthrough creates an S3 trigger named `fits-starfinder`, uploads FITS objects below `incoming/`, and gets one Slurm batch job per accepted object. Each job downloads its object and runs a mock `starfinder` command that writes a CSV catalog.

**`starfinder` is an illustrative mock, not software provided by EBS.** Supply a compatible executable on your compute system, or replace the command with software you have. All Project, bucket, machine, Username, and Account values below are synthetic; replace them with your own. No sample input FITS file is supplied (the companion guide shows how to make a small synthetic one).

## Prerequisites

1. Install the CLI and authenticate ([quick start](../README.md#quick-start), [authentication guide](authentication.md)):

   ```sh
   ebs auth login
   ```

   Use `ebs auth login --no-browser` on a machine without a browser.

2. Access to a Project with a provisioned input bucket and permission to manage triggers. Confirm the Project you will use:

   ```sh
   ebs projects list
   ebs --project example-project projects show
   ```

3. In the web interface's **Settings → SFAPI clients**, an unexpired client eligible to submit jobs, listed with its scheduler Username and available Accounts. You will use that Username and one of its Accounts in the YAML; never put a client secret or any credential in the document.

4. On the scheduler machine: `curl`, a writable `$SCRATCH`, and the mock `starfinder` executable (or your replacement) available to batch jobs.

## The trigger document

The complete example document is [the example YAML](../examples/fits-starfinder.yaml) in the CLI repository. Open it and replace every synthetic value — `example-project`, `example-input-bucket`, `example-scheduler`, `example-user`, `example-account` — with your own. For reference, the document contains:

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

How to read it:

- **Matchers:** all conditions must hold — an object-creation event in the selected bucket, a key beginning with `incoming/`, and a lowercase `.fits` or `.fit` suffix. So `incoming/run-42.fits` and `incoming/run-43.fit` each produce one event and one job.
- **`source: template`** stores this inline Jinja2 script in the trigger. Use `source: path` instead only to reference a script that already exists on the scheduler's filesystem; neither choice uploads a local file.
- **Slurm resources are static** (one node, one task, four CPUs, ten minutes); an S3 event does not carry resource values, so size the allocation in the script. **`{{ event.account }}`** is filled from the selected Account, which is why `sfapi_username` and `sfapi_account` are required here.
- **`{{ job.id }}`** is the EBS job record ID, not the scheduler job ID and not a field of the S3 event. It names each job's output directory: `$SCRATCH/starfinder/{{ job.id }}/`.
- **`{{ s3.presigned_url }}`** becomes a short-lived presigned GET URL for the triggering object. Treat it as a secret while valid, and note that it is created when the event is processed — a job queued past the URL's expiry fails at the `curl` step.

## Apply the trigger

From the repository root — the directory containing `examples/`:

```sh
ebs --project example-project triggers apply --file examples/fits-starfinder.yaml
ebs --project example-project triggers show fits-starfinder
```

**`apply` is a real create or full-state update, not a dry run.** With `enabled: true`, a successfully applied trigger can submit real jobs for matching uploads.

See [Creating and applying JSON or YAML documents](triggers.md#creating-and-applying-json-or-yaml-documents).

## Upload FITS objects

Upload a valid local FITS file to the exact matching key:

```sh
ebs --project example-project s3 upload fits-starfinder ./run-42.fits --key incoming/run-42.fits
```

To also exercise the other accepted suffix, upload a second file separately — each upload is one independent event and job:

```sh
ebs --project example-project s3 upload fits-starfinder ./run-43.fit --key incoming/run-43.fit
```

Verify the stored keys with a bounded listing:

```sh
ebs --project example-project s3 ls fits-starfinder --prefix incoming/ --limit 100
```

Listing proves the objects are stored, not that their jobs have finished. See [Upload a local file](s3.md#upload-a-local-file) and [List objects](s3.md#list-objects).

## Inspect events and jobs

```sh
ebs --project example-project events list fits-starfinder --source s3 --limit 50
```

Expect one row per accepted object, with the event status, **Job ID**, and job status. Use the **Job ID** — not the separate scheduler submission ID — in the next step. Add `--json` for machine-readable fields, and use the returned `--cursor` to page further. Submission and completion are asynchronous; poll again later if a job has not finished. See [Events and jobs](triggers.md#events-and-jobs).

## Inspect the catalog on the scheduler

After the job completes, on the scheduler replace `JOB_ID` with the event row's **Job ID**:

```sh
catalog="$SCRATCH/starfinder/JOB_ID/catalog.csv"
test -s "$catalog"
head -n 6 "$catalog"
```

If the file is missing, recheck the Job ID and that event's job status and failure reason before troubleshooting further.

For this mock's output convention, a catalog has the columns `source_id`, `ra_deg`, `dec_deg`, `flux`, and `snr` (identifier, coordinates in degrees, flux, and signal-to-noise ratio). This is invented sample content showing the shape, not output from a run:

```csv
source_id,ra_deg,dec_deg,flux,snr
MOCK-001,150.125000,2.250000,125.0,12.5
MOCK-002,150.130000,2.255000,80.0,9.0
```

Your replacement application defines its own columns and conventions — check its exit status, logs, and output against its documentation.

## Related guides

- [FITS pipeline with the web interface](fits-starfinder-web-ui.md)
- [Triggers and events](triggers.md)
- [S3 operations and boto3](s3.md)
- [Authentication and Projects](authentication.md)
- [Guide index](index.md)
