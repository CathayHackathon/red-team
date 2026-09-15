# Deploying to GCP

## Permissions the deploying identity needs

Whoever runs `deploy.sh` (i.e. whatever account your `gcloud auth login`
is using) needs these roles on the project — not Owner, but a specific set,
since the script does several distinct kinds of things:

| What the script does | Role needed |
|---|---|
| `gcloud services enable ...` | `roles/serviceusage.serviceUsageAdmin` |
| Create the results bucket, set its IAM | `roles/storage.admin` |
| Create the two service accounts | `roles/iam.serviceAccountAdmin` |
| Deploy Cloud Run services/Jobs with `--service-account=...` | `roles/iam.serviceAccountUser` (on those two SAs) |
| Create secrets, set their IAM | `roles/secretmanager.admin` |
| `gcloud run deploy` / `gcloud run jobs deploy --source` | `roles/run.admin` + `roles/cloudbuild.builds.editor` (source deploys build via Cloud Build) |
| Scope the trigger SA to `run.jobs.run` on just the one Job | `roles/run.admin` (covers `run.jobs.setIamPolicy`) |

The simplest bundle covering all of these is **Editor + `roles/run.admin` +
`roles/iam.serviceAccountAdmin` + `roles/iam.serviceAccountUser` +
`roles/secretmanager.admin`** — or just Owner if this is your own hackathon
project and you don't need to scope it down. If you're deploying as a
teammate rather than the project owner, ask whoever owns the project to
grant those roles (`gcloud projects add-iam-policy-binding PROJECT_ID
--member=user:you@example.com --role=roles/run.admin`, etc.) rather than
handing out Owner.

Worth calling out what I deliberately avoided: an earlier draft of this
script granted the trigger service's own service account `roles/run.developer`
at the **project** level so it could start the Job — that would have meant
the trigger SA (a small public-facing Flask app) could create, update, or
delete *any* Cloud Run service or Job in the project, not just run this one.
Fixed: it now gets `roles/run.invoker` bound to just the one Job resource
(`gcloud run jobs add-iam-policy-binding`), which only grants `run.jobs.run`
on that specific Job — if the trigger service is ever compromised, its blast
radius is "can start campaigns," not "can touch any Cloud Run resource in
the project." That's a different question from what *you* need to run the
deploy script (above) — these are the runtime permissions the deployed
services themselves hold, not yours.


I couldn't run this deployment from inside this Claude session — both the
cloud sandbox and the linked-computer bridge are blocked from reaching
Google Cloud APIs by an org-level egress policy (confirmed directly:
`curl` to `run.googleapis.com` gets a 403 from the proxy on both sides).
So this needs to run from a normal terminal with real internet access —
your own Mac Terminal (not through the Cowork device bridge) or Cloud
Shell — where your existing `gcloud` login already works.

## What gets deployed

- **`aegis-blue-team`** (Cloud Run service, public) — the AegisOps agent
  under test, running the exact same `MockAegisTarget` logic already
  validated in the offline demo, behind a Flask app. Protected by a bearer
  token in app code (Cloud Run IAM itself is `--allow-unauthenticated`,
  matching the "attack over the real public internet" design).
- **`aegis-redteam-job`** (Cloud Run Job) — the harness, running one
  campaign per execution against the blue-team's public URL, uploading
  findings/report/patch-proposals to a GCS bucket.
- **`aegis-trigger`** (Cloud Run service, public) — `POST /trigger` starts
  a Job execution; `GET /status/<execution>` polls it. Gated by a shared
  secret header, not GCP IAM, so any HTTPS caller can use it.

## Run it

```bash
cd aegis-redteam
export PROJECT_ID=cathay-ai-hackathon-team-02
export REGION=asia-east1
# optional — omit to run the job in offline mock-attacker mode:
export ANTHROPIC_API_KEY=sk-ant-...

./deploy/deploy.sh
```

It's idempotent — re-running it updates existing resources rather than
failing on "already exists." It prints the blue-team URL, the trigger URL,
and the generated trigger key at the end; save that key, you'll need it for
every `/trigger` call.

## Test it

The script prints ready-to-run `curl` commands at the end. In short:

```bash
curl -s -X POST "$TRIGGER_URL/trigger" \
  -H "X-Trigger-Key: $TRIGGER_KEY" -H "Content-Type: application/json" \
  -d '{"run_id": "smoke-test", "mode": "baseline", "max_turns": 3}'
# -> {"execution": "projects/.../executions/aegis-redteam-job-xxxxx", ...}

curl -s "$TRIGGER_URL/status/aegis-redteam-job-xxxxx" -H "X-Trigger-Key: $TRIGGER_KEY"
# -> {"state": "SUCCEEDED", "succeeded": 1, "failed": 0}

gcloud storage cp gs://<bucket>/runs/smoke-test/report.html ./report.html
```

You can also watch it happen in Cloud Logging while it runs:

```bash
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="aegis-redteam-job"' \
  --project="$PROJECT_ID" --limit=50 --format=json --freshness=10m
```

Every line is the same structured JSON the offline demo printed to your
terminal — `jsonPayload.event="finding_confirmed"`,
`jsonPayload.run_id="smoke-test"`, etc. — just now actually in Cloud
Logging instead of local stdout.

## Things I couldn't verify without live GCP access

I wrote and syntax-checked all of this, and re-ran the offline demo after
editing `target.py` to confirm nothing broke locally — but I could not
execute a single `gcloud` command against your project from within this
session (no network path exists there, as above). So there's a real chance
something needs a small fix on first run — most likely candidates, in
rough order of likelihood:

- **IAM propagation delay**: the `roles/run.developer` binding on the
  trigger service account can take a minute or two to become effective;
  if `/trigger` returns a 403/permission error immediately after deploy,
  wait ~60s and retry before assuming something's wrong.
- **`google-cloud-run` client API surface**: `operation.metadata.name` is
  how I'm getting the execution name back from `JobsClient.run_job()`
  without waiting for the job to finish — this matches the documented v2
  API shape, but if your installed client library version returns
  something slightly different, the trigger's `/trigger` endpoint will
  500 with the real exception message in the response body (paste it back
  to me and I'll fix it).
- **Cloud Build default service account permissions**: `--source` deploys
  build via Cloud Build automatically; if your project has tightened the
  default Cloud Build SA's permissions, the build step itself (not my
  code) may need an IAM grant — the error message from `gcloud run deploy`
  will say so explicitly if that's the issue.

Paste me any error output and I'll fix the code directly — I just can't
run the commands myself from here.
