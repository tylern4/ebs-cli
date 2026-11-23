# FITS upload and mock starfinder with the web interface

Return to the [guide index](index.md). For the same pipeline as a YAML document and terminal commands, use the [companion CLI walkthrough](fits-starfinder-cli.md).

This example uses the [EBS web interface](https://ebs-poc.nersc.gov) to watch a Project bucket for new FITS objects. Each accepted object creation produces one event and submits one Slurm batch job. The job downloads the triggering object through its event's presigned GET URL, invokes an explicitly mock `starfinder` command, and writes a CSV source catalog on the compute system.

**`starfinder` is an illustrative application interface, not software provided or installed by EBS.** Supply a compatible mock executable yourself or replace the command with software available on your compute system. The example names and catalog below are synthetic; they are not existing resources, output from an executed run, or scientific results.

> **Simulation versus real actions:** **Test run** only simulates an event and renders the script. **Create trigger** creates the real trigger, and the later upload can submit a real batch job.

## Prerequisites

You need:

- Membership in a Project with a provisioned bucket and permission to create a trigger. Maintainers and admins can manage all triggers in the Project; ordinary users can create and manage their own. Read-only staff cannot create one.
- A configured **Fire target** on which you may submit Slurm jobs, an eligible scheduler Username, and an Account available to that Username.
- An unexpired submission client stored in **Settings → SFAPI clients**. The list shows each client's Username, expiry, and available Accounts. If none is available, get your own client from Iris under **Profile**, then use **Add a client** in EBS Settings. Enter **Client ID (from Iris)** and **Client secret (the private-key JWK JSON from Iris)**, and choose **Validate and store**. Do not put either value in the job template.
- `curl`, a writable `$SCRATCH`, and your replacement for the mock `starfinder` executable available to batch jobs on that compute system. Add any modules, environment setup, constraints, or QoS directives that system requires.
- The `ebs` client installed in a local terminal and authenticated with `ebs auth login`. See the [installation quick start](../README.md#quick-start) and [authentication guide](authentication.md). This terminal step is required because the browser does not read a local FITS file.
- A valid local FITS image. No input FITS fixture is supplied with the documentation.

The example uses these replaceable values. Select real values offered by the web interface rather than typing the synthetic Username, Account, or fire target into another field.

| Purpose | Synthetic example value |
| --- | --- |
| Project | `example-project` |
| Trigger name | `fits-starfinder` |
| Project bucket | `example-input-bucket` |
| Fire target | `example-scheduler` |
| Scheduler Username | `example-user` |
| Scheduler Account | `example-account` |
| Local input | `./run-42.fits` |
| Object key | `incoming/run-42.fits` |

Use the exact provisioned bucket associated with the selected Project. The upload controls derive their destination from the trigger, so an unrelated bucket name is not a way to redirect the upload.

### Optional: create a small synthetic FITS image

If Astropy and NumPy are already installed in your local Python environment, the following creates `run-42.fits` in the current directory. It makes a 64 × 64 noise image with two bright pixels. It does not overwrite an existing file.

```sh
python - <<'PY'
import numpy as np
from astropy.io import fits

rng = np.random.default_rng(42)
image = rng.normal(0.0, 1.0, (64, 64))
image[31, 18] += 35.0
image[12, 47] += 24.0
fits.writeto("run-42.fits", image)
PY
```

This is only transportable example input. The mock command below does not establish a detection algorithm or validate this image scientifically.

## 1. Start a new trigger

1. Sign in to the web interface and select the intended Project in the header. See [Using the web interface](web-interface.md) for Project and role behavior.
2. Open **Triggers** and choose **New trigger**.
3. Set **Name** to `fits-starfinder`, leave **Version** as `v1`, and select the intended **Project** in the dialog. Keep **Enabled — turn off to pause this trigger** selected so the trigger is enabled after creation.
4. In **Template**, choose **File Create → Job Start**, then **Next**. This prefills an S3 object-creation input and a job-script output; both remain editable.

## 2. Configure the FITS input filters

In **Inputs**, confirm that **Input type** is **S3 bucket events**. Under **Filters**:

1. Under **S3 events**, select only `s3.object.created`. Do not select `s3.object.deleted`.
2. Choose **Bucket + file type** rather than **Object pattern** so each condition is visible separately.
3. Set the fields as follows:

   | Field | Value |
   | --- | --- |
   | **S3 bucket** | `example-input-bucket` — replace this with the selected Project's exact provisioned bucket |
   | **Path prefix** | `incoming/` |
   | **File types** | `.fits, .fit` |

These conditions correspond to input type `s3_bucket_events`, creation event `s3.object.created`, key prefix `incoming/`, and suffixes `.fits` or `.fit`. The two suffixes are alternatives: an object needs only one of them. Thus `incoming/run-42.fits` and `incoming/run-43.fit` are accepted, while an object outside `incoming/` or with another suffix is not.

Choose **Next**.

## 3. Configure the inline Slurm batch template

In **Outputs**:

1. Keep **Start a job from a script** selected.
2. Under **Fire target**, choose the actual compute system represented here by `example-scheduler`.
3. Under **Submission identity**:
   - For **SFAPI client**, select the stored, unexpired client for the intended scheduler Username (`example-user` in this synthetic example). **None — no SFAPI client selected (jobs cannot run)** is not sufficient for this walkthrough.
   - For **Default Account**, select an Account reported for that client (`example-account` here). An inline-template trigger with a selected client requires this binding. The selected value supplies `{{ event.account }}` when the script is rendered.
4. Under **Job script**, select **Template**, not **Path**. Replace the starter contents of the **Jinja2 template** editor with this complete script:

```bash
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

The directives statically request one node, one task, four CPUs, and ten minutes. An S3 upload event cannot supply Slurm resources such as `event.nodes`; keep those choices in the script. Adapt the directives and application command to the selected compute system.

For each accepted object, the rendered job makes a directory named with its EBS job record ID, downloads the object as `input.fits`, and asks the mock application to write `catalog.csv`. `set -euo pipefail` and the `curl` failure options stop the job rather than treating a failed download as a valid FITS input. The final `test -s catalog.csv` requires a nonempty output file, but does not validate its schema or scientific results. The object key is deliberately not interpolated into shell syntax: fixed local names avoid shell-metacharacter problems in user-controlled filenames.

File-event templates also provide values including `event.s3.key` and `filepath.name`, but this example does not need them. `job.id` is the EBS **Job record** ID shown in the browser, not the separate scheduler job ID.

**Template versus Path:** **Template** stores the inline Jinja2 text above on the trigger and renders it for each event. **Path** refers to a script that already exists on the scheduler-hosted filesystem and submits that reference unchanged. Neither choice uploads a local script from your browser or terminal.

### Understand the download URL before submitting

`{{ s3.presigned_url }}` becomes a short-lived, presigned GET URL for the triggering object when the real event is rendered. It grants object access until its configured expiry; it is not a public or permanent link. Do not echo it, paste it into tickets or chat, or share screenshots and rendered scripts while it is valid.

The URL is created when EBS processes the event, not when Slurm starts the job. A sufficiently long queue delay can therefore make the download fail with an expired URL. This example does not provide durable input staging or refresh the URL inside the queued job. Account for that limitation before using this pattern for real workloads.

The upload link discussed later is a different URL: it authorizes a PUT from your client, while this template's URL authorizes the batch job's GET.

Choose **Next**.

## 4. Test the template without submitting work

In **Test/Verify**, the dialog says **One simulated event; nothing submitted.** The sample **Object key** and **Bucket** derive from the filters; set the object key to `incoming/run-42.fits` if it is not already present.

1. Choose **Test run**. Check for **The event matches the filter.** and inspect the **Rendered script**. A filter mismatch or template render error means no real job should be attempted until you correct the configuration.
2. The test's download URL is a deterministic, non-bearer preview value, not access to your object. The test does not upload `run-42.fits`, contact Slurm, submit a batch job, create a catalog, or create the trigger.
3. Optionally choose **Validate** to check the form. A successful test or validation still leaves the dialog open and does not save it.
4. Choose **Create trigger** explicitly. This is the real create operation. The enabled trigger can now process matching objects.

Open the new `fits-starfinder` row to reach its detail page. If its status is **Not deployed yet** and **Deploy now** is offered, an authorized Project maintainer or admin must deploy it before uploads can be processed.

## 5. Upload the FITS object from the terminal

The trigger detail page has an **Upload data** section for S3 triggers you may manage. It offers **Presigned upload link** and **Temporary upload credentials**. Both grant short-lived, upload-only access and both kinds of values are secrets until they expire. Keep them masked and out of logs, tickets, chat, and shared terminals.

**There is no browser local-file picker or drag-and-drop FITS upload.** The browser grants upload access and provides a command; it does not open or read `./run-42.fits`. The terminal client reads the local file and transfers it directly to the trigger's bucket.

This walkthrough uses the secret-free **Direct upload** command, so you do not need to reveal or copy the signed URL or temporary credential values:

1. Under **Presigned upload link**, enter **Object key** `incoming/run-42.fits`.
2. Select a suitable **Link lifetime** and choose **Generate upload link**. Generating the link does not upload a file. The link permits PUT uploads of that exact key and is replayable until expiry; it is not a one-time link.
3. In the generated result, find **Direct upload** and choose its **Copy** button. The copied command contains the selected Project, trigger ID, object key, and the placeholder `/path/to/your-file`, but no signed URL or credential.
4. In the authenticated terminal, change to the directory containing `run-42.fits`. Paste the copied command and replace only `/path/to/your-file` with `./run-42.fits`. Keep the copied Project, trigger ID, and `--key 'incoming/run-42.fits'` intact, then run it.

For comparison, the resulting command has this shape; the actual copied command and its trigger ID are authoritative:

```sh
ebs --project 'example-project' s3 upload 'TRIGGER_ID_FROM_THE_COPIED_COMMAND' ./run-42.fits --key 'incoming/run-42.fits'
```

The CLI obtains its own short-lived, operation-scoped upload credentials and does not print them. It does not use or display the browser-generated signed PUT URL. A successful upload reports the `s3://…/incoming/run-42.fits` target. See [S3 operations and boto3](s3.md) for direct-upload behavior and the other upload-access options.

Creating `incoming/run-42.fits` now matches the trigger and produces one event with one associated batch-job submission. A separate FITS upload is a separate event and job; do not expect multiple objects to be combined into one catalog.

## 6. Follow the event and job in the browser

Return to the `fits-starfinder` detail page and its **Events & jobs** table.

- Find the row whose **Object** ends in `incoming/run-42.fits`.
- Check the event **Status** and the **Job** state. Expand the row to see the submission outcome, scheduler details, any bounded failure reason, the **Job record** ID, and the rendered script.
- **Waiting to be submitted** means no scheduler job exists yet. **Submitted** means the scheduler accepted a job; it does not by itself mean the job finished. A failed row includes its recorded failure reason when one is available.
- Treat the rendered script as sensitive while its presigned GET URL remains valid.

If no event arrives, verify that the upload completed, the trigger is enabled and running, and the bucket, creation-event, prefix, and lowercase suffix filters match exactly. If the job failed, expand the row before troubleshooting; an expired download URL after a long queue wait is one possible cause.

A completed EBS job state is not evidence that `starfinder` produced a catalog or that any detections are scientifically valid. Inspect the file and validate the application results on the compute system.

## 7. Inspect the source catalog on the compute system

Open a shell on the selected compute system. Copy the **Job record** ID from the expanded event—not the **SFAPI job id**—and set it here:

```sh
JOB_RECORD_ID='PASTE_THE_JOB_RECORD_ID'
catalog="$SCRATCH/starfinder/$JOB_RECORD_ID/catalog.csv"
ls -l "$catalog"
cat "$catalog"
```

For this example's mock output convention, a catalog has the following columns:

| Column | Meaning |
| --- | --- |
| `source_id` | Catalog-local identifier for one detected source |
| `ra_deg`, `dec_deg` | Right ascension and declination in degrees |
| `flux` | Instrumental, background-subtracted source flux in the application's chosen units |
| `snr` | Detection signal-to-noise ratio; the illustrative command uses a threshold of 5 |

The following is **invented sample content only**. It illustrates the expected shape; it was not produced by running this walkthrough and is not a scientific detection claim.

```csv
source_id,ra_deg,dec_deg,flux,snr
MOCK-001,150.125000,2.250000,125.0,12.5
MOCK-002,150.130000,2.255000,80.0,9.0
```

Your replacement application defines its own actual algorithm, flux units, coordinate convention, columns, and quality checks. Check its exit status and logs, confirm that `catalog.csv` exists and is nonempty, and validate its contents against the input image before using the results.

## Related guides

- [FITS pipeline with the CLI](fits-starfinder-cli.md)
- [Using the web interface](web-interface.md)
- [Triggers and events](triggers.md)
- [S3 operations and boto3](s3.md)
- [Guide index](index.md)
