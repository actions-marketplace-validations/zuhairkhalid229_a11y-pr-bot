Written for: you, following this at a terminal on first deploy. Every value you must substitute is a shell variable set in step 0.

# First deploy

## The ordering constraint

Three services reference each other, so there is exactly one order that does not
require going back:

```
worker (get URL) → api (needs WORKER_BASE_URL) → dashboard (needs api URL)
                                   ↑                        │
                                   └─ DASHBOARD_ORIGINS ────┘   (one revisit, unavoidable)
```

The final revisit to the api is unavoidable — CORS cannot know the Vercel URL
before Vercel assigns it. Budget for it rather than being surprised.

## 0. Variables and prerequisites

```bash
export PROJECT=your-project-id
export REGION=europe-west1          # keep Firestore, Tasks, Run in one region
export FIRESTORE_LOCATION=eur3      # multi-region; cannot be changed later
export GITHUB_APP_ID=123456
export APP_SLUG=a11y-pr-bot
export DOMAIN=a11y.yourdomain.com
export PEM=./a11y-pr-bot.private-key.pem

gcloud config set project $PROJECT
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudtasks.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  iamcredentials.googleapis.com identitytoolkit.googleapis.com
```

`identitytoolkit` is Firebase Auth — the dashboard's sign-in fails silently
without it. Gemini needs no API enabled; it authenticates with the API key.

## 1. Build both images

The pins moved on Day 6 (`pydantic` 2.13.1, `google-cloud-firestore` 2.32.0,
`google-cloud-tasks` 2.25.0, `firebase-admin` 7.7.0 — the Python package, *not*
the npm one), so **do not reuse a cached image from before that.** Verify
locally first; a dependency conflict is much cheaper to find here than in Cloud
Build:

```bash
docker build --no-cache -t a11y-api:local .
docker run --rm a11y-api:local pip check          # must print "No broken requirements found"

docker build --no-cache -f worker/Dockerfile -t a11y-worker:local .
docker run --rm a11y-worker:local pip check
docker run --rm a11y-worker:local python -c "
from worker.axe import load_axe; print('axe', load_axe()[1])"
docker run --rm a11y-worker:local python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); print('chromium', b.version); b.close()"
```

The worker image builds from the **repo root** with `-f worker/Dockerfile`
because it needs both `worker/` and `app/` in context. The Playwright base image
tag must match `playwright==1.62.0` in `worker/requirements.txt` — a mismatch
fails at container start with "Executable doesn't exist", not at build time.

Then push:

```bash
gcloud artifacts repositories create a11y \
  --repository-format=docker --location=$REGION

gcloud builds submit --config worker/cloudbuild.yaml \
  --substitutions=_REGION=$REGION .

gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/a11y/api:latest .
```

## 2. Firestore, secrets, queue

```bash
gcloud firestore databases create --location=$FIRESTORE_LOCATION

gcloud secrets create github-private-key --data-file=$PEM
printf '%s' "$(openssl rand -hex 32)" | \
  gcloud secrets create github-webhook-secret --data-file=-
python -c "from app.crypto import SecretBox; print(SecretBox.generate_key())" | \
  tr -d '\n' | gcloud secrets create app-encryption-key --data-file=-
printf '%s' "$GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-

gcloud secrets versions access latest --secret=github-webhook-secret   # copy this
```

Copy that webhook secret now — you paste it into the GitHub App in step 6, and
reading it later is an extra step.

`app-encryption-key` is a Fernet key. **If you lose it, every stored Vercel
bypass token becomes undecryptable** and users must re-enter them. Losing it is
not a disaster, but note it somewhere durable.

```bash
gcloud tasks queues create a11y-scans --location=$REGION
# The worker answers 503 for transient failures and gives up itself after 4
# attempts, so cap the queue just above that.
gcloud tasks queues update a11y-scans --location=$REGION \
  --max-attempts=6 --min-backoff=30s --max-backoff=300s --max-concurrent-dispatches=10
```

## 3. Service accounts and every IAM binding

Three identities, each with the narrowest role that works. Default compute
service accounts are Editor on the whole project — do not use them.

```bash
gcloud iam service-accounts create a11y-api     --display-name="a11y api"
gcloud iam service-accounts create a11y-worker  --display-name="a11y worker"
gcloud iam service-accounts create tasks-invoker --display-name="Cloud Tasks OIDC identity"

export SA_API=a11y-api@$PROJECT.iam.gserviceaccount.com
export SA_WORKER=a11y-worker@$PROJECT.iam.gserviceaccount.com
export SA_TASKS=tasks-invoker@$PROJECT.iam.gserviceaccount.com
```

**api** — Firestore, enqueue tasks, and permission to *attach* the tasks-invoker
identity to a task it creates:

```bash
for role in roles/datastore.user roles/cloudtasks.enqueuer; do
  gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$SA_API" --role="$role"
done
gcloud iam service-accounts add-iam-policy-binding $SA_TASKS \
  --member="serviceAccount:$SA_API" --role="roles/iam.serviceAccountUser"
```

That last binding is the one people miss. Without it every enqueue fails with a
permission error on the OIDC token, not on the task.

**worker** — Firestore only; it never enqueues:

```bash
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA_WORKER" --role="roles/datastore.user"
```

**Secrets**, per secret, per consumer. The api verifies webhooks and encrypts
bypass tokens; the worker signs GitHub calls, decrypts bypass tokens, and calls
Gemini. The worker also needs `github-webhook-secret` even though it verifies
nothing — `app/config.Settings` declares it required, so the container will not
start without it:

```bash
for s in github-private-key github-webhook-secret app-encryption-key; do
  for m in $SA_API $SA_WORKER; do
    gcloud secrets add-iam-policy-binding $s \
      --member="serviceAccount:$m" --role="roles/secretmanager.secretAccessor"
  done
done
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:$SA_WORKER" --role="roles/secretmanager.secretAccessor"
```

## 4. Deploy the worker (private)

```bash
gcloud run deploy a11y-worker \
  --image $REGION-docker.pkg.dev/$PROJECT/a11y/worker:latest \
  --region $REGION --service-account $SA_WORKER \
  --no-allow-unauthenticated \
  --memory 2Gi --cpu 2 --concurrency 2 --timeout 600 \
  --min-instances 0 --max-instances 5 \
  --set-env-vars "GITHUB_APP_ID=$GITHUB_APP_ID,GCP_PROJECT_ID=$PROJECT,GEMINI_MODEL=gemini-3.1-flash-lite,MAPPER_MAX_FINDINGS=10" \
  --set-secrets "GITHUB_PRIVATE_KEY=github-private-key:latest,GITHUB_WEBHOOK_SECRET=github-webhook-secret:latest,APP_ENCRYPTION_KEY=app-encryption-key:latest,GEMINI_API_KEY=gemini-api-key:latest"

export WORKER_URL=$(gcloud run services describe a11y-worker --region $REGION --format='value(status.url)')

gcloud run services add-iam-policy-binding a11y-worker --region $REGION \
  --member="serviceAccount:$SA_TASKS" --role="roles/run.invoker"
```

`--concurrency 2` must match `WORKER_MAX_CONCURRENT_SCANS` (default 2). Higher
concurrency than the semaphore just queues requests inside the container and
risks the 600s timeout; lower wastes instances.

**Do not switch `GEMINI_MODEL` without re-running `scripts/eval_mapper.py`.**
`gemini-3.1-flash-lite` is the measured one: 0 wrong suggestions, 100% location
precision on the 12-case eval. `gemini-3.5-flash` is capped at 20 requests/day
on the free tier, which one busy PR can exhaust.

## 5. Deploy the api (public)

```bash
gcloud run deploy a11y-api \
  --image $REGION-docker.pkg.dev/$PROJECT/a11y/api:latest \
  --region $REGION --service-account $SA_API \
  --allow-unauthenticated \
  --no-cpu-throttling \
  --memory 512Mi --cpu 1 --concurrency 40 --timeout 60 \
  --min-instances 0 --max-instances 3 \
  --set-env-vars "GITHUB_APP_ID=$GITHUB_APP_ID,GCP_PROJECT_ID=$PROJECT,ENABLE_CLOUD_TASKS=true,TASKS_LOCATION=$REGION,TASKS_QUEUE=a11y-scans,WORKER_BASE_URL=$WORKER_URL,TASKS_INVOKER_SA=$SA_TASKS,PREVIEW_WAIT_MINUTES=15,DASHBOARD_ORIGINS=https://$DOMAIN" \
  --set-secrets "GITHUB_PRIVATE_KEY=github-private-key:latest,GITHUB_WEBHOOK_SECRET=github-webhook-secret:latest,APP_ENCRYPTION_KEY=app-encryption-key:latest"

export API_URL=$(gcloud run services describe a11y-api --region $REGION --format='value(status.url)')
curl -s $API_URL/healthz     # {"status":"ok"}
```

**`--no-cpu-throttling` is not optional.** The webhook returns 202 and then
creates the Check Run in a background task. With default throttling Cloud Run
freezes CPU the moment the response is sent, and that task stalls until the next
request happens to wake the instance — so checks appear minutes late or not at
all, intermittently, which is miserable to debug.

`--allow-unauthenticated` is correct here: GitHub cannot present a Google
identity. The endpoint's real auth is the HMAC signature.

## 6. GitHub App: flip from smee to production

**Settings → Developer settings → GitHub Apps → your app.** Four fields:

| Field | Value |
|---|---|
| Webhook URL | `$API_URL/webhooks/github` |
| Webhook secret | the `github-webhook-secret` value from step 2 |
| Homepage URL | `https://$DOMAIN` |
| Setup URL | `https://$DOMAIN/setup` · **Redirect on update** ticked |
| Callback URL | `https://$DOMAIN/api/auth/github/callback` |

Leave *Request user authorization (OAuth) during installation* **off** — it adds
a consent screen to every install.

Confirm the five repository permissions are still exactly: Checks read+write,
Pull requests read+write, Contents read, **Deployments read**, Metadata read.
Deployments is the one to double-check — without it `deployment_status` never
arrives, every scan times out after 15 minutes into `no_preview`, and nothing in
the logs looks like an error.

Events: `Pull request`, `Deployment status`, `Check run`.

Then **Advanced → Recent Deliveries → Redeliver** the most recent `ping`. A 204
means signature verification works end to end. Do this before touching a real
PR; it isolates webhook plumbing from everything else.

### Firebase Auth

**Firebase Console → Authentication → Sign-in method → GitHub.** Enable it and
paste the app's OAuth **Client ID and Client secret** (GitHub App settings page,
not the private key). Copy the callback URL Firebase shows you and ensure it
matches the Callback URL above. Then **Authentication → Settings → Authorized
domains** → add `$DOMAIN`. Sign-in fails with an unhelpful error if that domain
is missing.

## 7. Firestore rules and indexes

```bash
npm i -g firebase-tools
firebase login
firebase use $PROJECT
firebase deploy --only firestore:rules,firestore:indexes

gcloud firestore fields ttls update expires_at \
  --collection-group=deliveries --enable-ttl
```

Deploy these **before** the dashboard. Without the composite indexes every scan
query fails with a missing-index error (the message contains a link that creates
it, but `firestore.indexes.json` is the version-controlled answer). Index builds
take a few minutes on an empty database and far longer later.

The TTL on `deliveries` matters: it is written once per webhook and never read
after 7 days. Without TTL it grows forever.

## 8. Deploy the dashboard

```bash
cd web
cp .env.example .env.local     # fill in Firebase web config + API_URL + APP_SLUG
npm install && npm run build   # catch it locally, not in Vercel's build log

npm i -g vercel
vercel link
for k in NEXT_PUBLIC_FIREBASE_API_KEY NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN \
         NEXT_PUBLIC_FIREBASE_PROJECT_ID NEXT_PUBLIC_API_BASE_URL NEXT_PUBLIC_APP_SLUG; do
  vercel env add $k production
done
vercel --prod
```

`NEXT_PUBLIC_API_BASE_URL` is `$API_URL`. The Firebase web values are public by
design — access is controlled by `firestore.rules`, not by hiding them.

Point `$DOMAIN` at the Vercel deployment (Vercel → Settings → Domains), then
close the loop:

```bash
gcloud run services update a11y-api --region $REGION \
  --update-env-vars "DASHBOARD_ORIGINS=https://$DOMAIN,https://<project>.vercel.app"
```

Include the `*.vercel.app` URL too, or preview deployments of your own dashboard
cannot call the api.

## 9. Verify

```bash
export GH_TOKEN=$(gh auth token)
python scripts/smoke_test.py --repo <owner>/<repo>
```

Install the app on a throwaway repo with a working Vercel preview first. The
smoke test walks every stage and names the one that broke. Read
`scripts/smoke_test.py --help` for the flags.

## Rollback

```bash
gcloud run revisions list --service a11y-api --region $REGION
gcloud run services update-traffic a11y-api --region $REGION --to-revisions <REVISION>=100
```

To stop all scanning immediately without touching revisions, pause the queue —
in-flight tasks stop being dispatched and nothing is lost:

```bash
gcloud tasks queues pause a11y-scans --location=$REGION
```

## What to watch in the first hour

```bash
gcloud run services logs tail a11y-api    --region $REGION
gcloud run services logs tail a11y-worker --region $REGION
```

Logs are structured JSON. The lines that matter:

| Log event | Means |
|---|---|
| `webhook_accepted` | signature verified, work queued |
| `invalid_signature` | the webhook secret does not match step 2 |
| `check_run_created` | GitHub write permissions are good |
| `deployment_registered` `provider=vercel` | preview discovery works |
| `scan_transition to=queued applied=true` | both halves merged; **`applied=false` repeatedly means the state machine is wedged** |
| `scan_complete findings=N` | the scanner works |
| `mapping_complete suggestions=N` | Gemini is reachable and mapping |
| `mapper_retry` | Gemini 503/429; expected occasionally, constant means quota |
| `post_complete posted=N rejected=M` | **`rejected` > 0 means comment anchors are being refused** — investigate before it becomes a habit |
| `quota_checked allowed=false` | plan limit hit |
