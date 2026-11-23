# FITS upload and mock starfinder with the web interface

Return to the [guide index](index.md). For the same pipeline as a YAML document and terminal commands, use the [companion CLI walkthrough](fits-starfinder-cli.md). Both walkthroughs share one complete trigger document: [the example YAML](../examples/fits-starfinder.yaml).

This walkthrough uses the [EBS web interface](https://ebs-poc.nersc.gov) to watch a Project bucket for new FITS objects. Each accepted object creation produces one event and submits one Slurm batch job, which downloads the object and runs a mock `starfinder` command that writes a CSV catalog.

**`starfinder` is an illustrative mock, not software provided by EBS.** Supply a compatible executable on your compute system, or replace the command with software you have. All names and the sample catalog below are synthetic.

## Prerequisites

- Membership in a Project with a provisioned bucket and permission to create triggers.
- A configured **Fire target** on which you may submit Slurm jobs, plus an eligible scheduler Username and an Account available to that Username.
- An unexpired submission client under **Settings → SFAPI clients**. If you have none, get a client from Iris under **Profile**, then use **Add a client** in EBS Settings with **Validate and store**.
- `curl`, a writable `$SCRATCH`, and the mock `starfinder` executable (or your replacement) available to batch jobs on that compute system.
- The `ebs` client installed and authenticated with `ebs auth login` ([quick start](../README.md#quick-start), [authentication guide](authentication.md)). Required because the browser cannot read a local file.
- A valid local FITS image. No sample input is supplied with the documentation.

Replaceable values used below:

| Purpose | Example value |
| --- | --- |
| Project | `example-project` |
| Trigger name | `fits-starfinder` |
| Project bucket | `example-input-bucket` |
| Fire target | `example-scheduler` |
| Scheduler Username / Account | `example-user` / `example-account` |
| Local input → object key | `./run-42.fits` → `incoming/run-42.fits` |

Replace each example value with your own. The upload destination comes from the trigger's own Project bucket.

Optional: if you have Astropy and NumPy, this creates the small example input `run-42.fits` (a 64 × 64 noise image with two bright pixels); it does not overwrite an existing file:

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

## 1. Create the trigger

1. Sign in, select your Project in the header, open **Triggers**, and choose **New trigger**.
2. Set **Name** to `fits-starfinder`, keep **Version** `v1`, and select the Project. Keep **Enabled** selected so the trigger is active after creation.
3. In **Template**, choose **File Create → Job Start**, then **Next**. This prefills an S3 object-creation input and a job-script output.

## 2. Configure the FITS input

In **Inputs**, confirm **Input type** is **S3 bucket events**. Under **Filters**:

1. Under **S3 events**, select only `s3.object.created`.
2. Choose **Bucket + file type** and set:

   | Field | Value |
   | --- | --- |
   | **S3 bucket** | the selected Project's exact provisioned bucket |
   | **Path prefix** | `incoming/` |
   | **File types** | `.fits, .fit` |

The two suffixes are alternatives, so `incoming/run-42.fits` and `incoming/run-43.fit` both match. Choose **Next**.

## 3. Configure the batch job

In **Outputs**:

1. Keep **Start a job from a script** and choose your **Fire target**.
2. Under **Submission identity**, select the stored, unexpired **SFAPI client** for the intended scheduler Username and one of its available Accounts as **Default Account**. An inline-template trigger requires both; **None — no SFAPI client selected (jobs cannot run)** is not sufficient.
3. Under **Job script**, select **Template** (not **Path**) and replace the starter contents of the **Jinja2 template** editor with:

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

Notes on this script:

- Set the Slurm resources here: this example requests one node, one task, four CPUs, and ten minutes. S3 events do not supply resource values.
- Each job downloads its object and writes `catalog.csv` under `$SCRATCH/starfinder/{{ job.id }}/`. `job.id` is the EBS **Job record** ID shown in the browser—not the scheduler job ID or an S3 event field. The final `test -s` fails the job if the catalog is missing or empty.
- **Template** stores this inline script on the trigger; **Path** refers to a script that already exists on the scheduler's filesystem. Neither choice uploads a local file.
- `{{ s3.presigned_url }}` becomes a short-lived presigned GET URL for the triggering object. Treat it as a secret while valid: keep it out of logs, tickets, chat, and screenshots.

## 4. Test, create, and deploy

In **Test/Verify** (the dialog says **One simulated event; nothing submitted**), set the sample **Object key** to `incoming/run-42.fits` if needed.

1. Choose **Test run** and confirm **The event matches the filter.**, then check the **Rendered script**. A test does not upload a file or submit anything.
2. Choose **Create trigger**. This is the real create operation — the enabled trigger can now submit real jobs for matching uploads.
3. Open the new trigger's detail page. If its status shows **Not started** with **Deploy now** offered, a Project maintainer or admin must deploy it before uploads are processed.

## 5. Upload the FITS object

The trigger detail page has an **Upload data** section. **There is no browser file picker or drag-and-drop upload** — the browser only provides an upload command that you run in your authenticated terminal.

1. Under **Presigned upload link**, enter **Object key** `incoming/run-42.fits`, choose a **Link lifetime**, and choose **Generate upload link**. This only creates a link; it does not upload anything.
2. In the result, choose **Copy** on **Direct upload**. The copied command contains the Project, trigger ID, and object key — no secrets.
3. In the terminal, replace only `/path/to/your-file` with `./run-42.fits` and run it. The shape:

```sh
ebs --project 'example-project' s3 upload 'TRIGGER_ID_FROM_THE_COPIED_COMMAND' ./run-42.fits --key 'incoming/run-42.fits'
```

The CLI uploads the file to the trigger's bucket using its own short-lived credentials and reports the `s3://…/incoming/run-42.fits` target. Each uploaded object is one event and one separate job. See [S3 operations and boto3](s3.md).

## 6. Watch the event and job

On the trigger's **Events & jobs** table, find the row with **Object** `incoming/run-42.fits` and check the event **Status** and **Job** state; expand the row for the submission outcome, failure reason if any, the **Job record** ID, and the rendered script.

- **Waiting to be submitted** means no scheduler job exists yet; **Submitted** means the scheduler accepted one, not that it finished.
- If no event arrives, check that the upload completed, the trigger is enabled and deployed, and the bucket, prefix, and suffix filters match exactly.
- One caution about queue delays: the presigned URL was created when the event was processed, so a job that sits in the queue past the URL's expiry fails at the `curl` step with a recorded failure reason.

A completed job state does not prove the catalog's contents are valid — inspect the file next.

## 7. Inspect the catalog on the compute system

On the compute system, copy the **Job record** ID from the expanded event and run:

```sh
JOB_RECORD_ID='PASTE_THE_JOB_RECORD_ID'
catalog="$SCRATCH/starfinder/$JOB_RECORD_ID/catalog.csv"
ls -l "$catalog"
cat "$catalog"
```

For this mock's output convention, a catalog has the columns `source_id`, `ra_deg`, `dec_deg`, `flux`, and `snr` (identifier, coordinates in degrees, flux, and signal-to-noise ratio). This is invented sample content showing the shape, not output from a run:

```csv
source_id,ra_deg,dec_deg,flux,snr
MOCK-001,150.125000,2.250000,125.0,12.5
MOCK-002,150.130000,2.255000,80.0,9.0
```

Your replacement application defines its own columns and conventions — check its exit status, logs, and output against its documentation.

## Related guides

- [FITS pipeline with the CLI](fits-starfinder-cli.md)
- [Using the web interface](web-interface.md)
- [Triggers and events](triggers.md)
- [S3 operations and boto3](s3.md)
- [Guide index](index.md)
