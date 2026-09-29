<#
.SYNOPSIS
  Deploys the whole stack: both Cloud Run services, Firestore rules and
  indexes, and the Vercel dashboard.

.DESCRIPTION
  Every step is idempotent -- it checks whether the resource exists before
  creating it -- so this is safe to re-run after a failure, and safe to run
  again to push an update. Nothing here needs local Docker: images are built by
  Cloud Build.

  Order is forced by the services referencing each other:
    worker (get URL) -> api (needs it) -> dashboard (needs api) -> api CORS

.EXAMPLE
  .\scripts\deploy.ps1 -Project my-gcp-project -Domain a11y.example.com

.EXAMPLE
  .\scripts\deploy.ps1 -Project my-gcp-project -Domain a11y.example.com -Only api
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Project,
  [Parameter(Mandatory)][string]$Domain,
  [string]$Region = "europe-west1",
  [string]$FirestoreLocation = "eur3",
  [ValidateSet("all", "enable", "secrets", "iam", "build", "worker", "api", "firebase", "web", "cors")]
  [string]$Only = "all",
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root "tools"

# Project-local CLIs, so nothing depends on what is or is not on PATH.
$gcloud = Join-Path $tools "google-cloud-sdk\bin\gcloud.cmd"
$firebase = Join-Path $tools "node_modules\.bin\firebase.cmd"
$vercel = Join-Path $tools "node_modules\.bin\vercel.cmd"

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "    $text" -ForegroundColor DarkGray }
function Ok($text) { Write-Host "    $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Should($name) { return $Only -eq "all" -or $Only -eq $name }

# gcloud writes progress to stderr, which PowerShell turns into errors. Run it
# with $ErrorActionPreference relaxed and inspect the exit code instead.
function G {
  param([Parameter(ValueFromRemainingArguments)][string[]]$Args)
  $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
  try { $out = & $gcloud @Args 2>&1 | Out-String } finally { $ErrorActionPreference = $prev }
  return [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0); Out = $out.Trim() }
}

function Exists($checkArgs) { return (G @checkArgs).Ok }

# ---------------------------------------------------------------- preflight

Step "Preflight"
foreach ($pair in @(@("gcloud", $gcloud), @("firebase", $firebase), @("vercel", $vercel))) {
  if (-not (Test-Path $pair[1])) { throw "$($pair[0]) not found at $($pair[1]). Run: npm --prefix tools install" }
}
Ok "CLIs present (all project-local, under tools\)"

$account = (G config get-value account).Out
if (-not $account -or $account -match "unset") {
  throw "Not signed in. Run:  $gcloud auth login   (and then: $gcloud auth application-default login)"
}
Ok "authenticated as $account"

$null = G config set project $Project
$billing = G beta billing projects describe $Project --format="value(billingEnabled)"
if ($billing.Ok -and $billing.Out -notmatch "True") {
  throw "Billing is not enabled on '$Project'. Cloud Run cannot deploy without it, even inside the free tier."
}
Ok "project $Project"

# ------------------------------------------------------------------ enable

if (Should "enable") {
  Step "Enabling APIs (slow the first time)"
  $apis = @("run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
    "cloudtasks.googleapis.com", "firestore.googleapis.com", "secretmanager.googleapis.com",
    "iamcredentials.googleapis.com", "identitytoolkit.googleapis.com")
  $r = G services enable @apis
  if (-not $r.Ok) { throw "could not enable APIs:`n$($r.Out)" }
  Ok "enabled: $($apis.Count) APIs"

  Step "Firestore"
  if (Exists @("firestore", "databases", "describe", "--database=(default)")) {
    Info "database already exists"
  }
  else {
    $r = G firestore databases create --location=$FirestoreLocation
    if (-not $r.Ok) { throw "firestore create failed:`n$($r.Out)" }
    Ok "created in $FirestoreLocation"
  }

  Step "Cloud Tasks queue"
  if (Exists @("tasks", "queues", "describe", "a11y-scans", "--location=$Region")) {
    Info "queue already exists"
  }
  else {
    $null = G tasks queues create a11y-scans --location=$Region
    Ok "created a11y-scans"
  }
  # The worker gives up after 4 attempts itself; cap the queue just above that.
  $null = G tasks queues update a11y-scans --location=$Region `
    --max-attempts=6 --min-backoff=30s --max-backoff=300s --max-concurrent-dispatches=10
  Ok "retry policy set"

  Step "Artifact Registry"
  if (Exists @("artifacts", "repositories", "describe", "a11y", "--location=$Region")) {
    Info "repository already exists"
  }
  else {
    $null = G artifacts repositories create a11y --repository-format=docker --location=$Region
    Ok "created"
  }
}

# ----------------------------------------------------------------- secrets

if (Should "secrets") {
  Step "Secrets"
  $envFile = Join-Path $root ".env"
  if (-not (Test-Path $envFile)) { throw ".env not found. Run scripts/create_github_app.py first." }
  $envMap = @{}
  Get-Content $envFile | Where-Object { $_ -match "^\s*[A-Z_]+=" } | ForEach-Object {
    $k, $v = $_ -split "=", 2; $envMap[$k.Trim()] = $v.Trim()
  }

  function PutSecret($name, $value) {
    if (-not $value) { Warn "$name has no value; skipped"; return }
    $tmp = New-TemporaryFile
    try {
      # -NoNewline matters: a trailing newline inside a key or token breaks it.
      [IO.File]::WriteAllText($tmp.FullName, $value)
      if (Exists @("secrets", "describe", $name)) {
        $null = G secrets versions add $name --data-file=$($tmp.FullName)
        Info "$name : new version added"
      }
      else {
        $null = G secrets create $name --data-file=$($tmp.FullName)
        Ok "$name : created"
      }
    }
    finally { Remove-Item $tmp.FullName -Force -ErrorAction SilentlyContinue }
  }

  # The PEM is stored decoded: Secret Manager holds bytes happily and the app
  # accepts either form.
  $pem = $envMap["GITHUB_PRIVATE_KEY"]
  if ($pem -and $pem -notmatch "BEGIN") {
    try { $pem = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($pem)) } catch {}
  }
  PutSecret "github-private-key" $pem
  PutSecret "github-webhook-secret" $envMap["GITHUB_WEBHOOK_SECRET"]

  # Generated once and never rotated casually: losing it makes every stored
  # Vercel bypass token undecryptable.
  if (-not (Exists @("secrets", "describe", "app-encryption-key"))) {
    $key = & (Join-Path $root ".venv\Scripts\python.exe") -c "from app.crypto import SecretBox; print(SecretBox.generate_key())"
    PutSecret "app-encryption-key" $key.Trim()
  }
  else { Info "app-encryption-key : already exists (not rotating)" }

  PutSecret "gemini-api-key" $envMap["GEMINI_API_KEY"]
}

# --------------------------------------------------------------------- iam

$saApi = "a11y-api@$Project.iam.gserviceaccount.com"
$saWorker = "a11y-worker@$Project.iam.gserviceaccount.com"
$saTasks = "tasks-invoker@$Project.iam.gserviceaccount.com"

if (Should "iam") {
  Step "Service accounts"
  foreach ($pair in @(@("a11y-api", "a11y api"), @("a11y-worker", "a11y worker"), @("tasks-invoker", "Cloud Tasks OIDC identity"))) {
    if (Exists @("iam", "service-accounts", "describe", "$($pair[0])@$Project.iam.gserviceaccount.com")) {
      Info "$($pair[0]) exists"
    }
    else {
      $null = G iam service-accounts create $pair[0] --display-name=$pair[1]
      Ok "created $($pair[0])"
    }
  }

  Step "IAM bindings"
  foreach ($b in @(@($saApi, "roles/datastore.user"), @($saApi, "roles/cloudtasks.enqueuer"),
      @($saWorker, "roles/datastore.user"))) {
    $null = G projects add-iam-policy-binding $Project --member="serviceAccount:$($b[0])" --role=$b[1] --condition=None
    Info "$($b[1]) -> $($b[0].Split('@')[0])"
  }

  # The binding people miss. Without it every enqueue fails on the OIDC token,
  # which reads like a Cloud Tasks problem and is not one.
  $null = G iam service-accounts add-iam-policy-binding $saTasks `
    --member="serviceAccount:$saApi" --role="roles/iam.serviceAccountUser"
  Ok "api may impersonate tasks-invoker"

  foreach ($s in @("github-private-key", "github-webhook-secret", "app-encryption-key")) {
    foreach ($m in @($saApi, $saWorker)) {
      $null = G secrets add-iam-policy-binding $s --member="serviceAccount:$m" --role="roles/secretmanager.secretAccessor"
    }
  }
  if (Exists @("secrets", "describe", "gemini-api-key")) {
    $null = G secrets add-iam-policy-binding gemini-api-key --member="serviceAccount:$saWorker" --role="roles/secretmanager.secretAccessor"
  }
  Ok "secret access granted"
}

# ------------------------------------------------------------------- build

$registry = "$Region-docker.pkg.dev/$Project/a11y"
if ((Should "build") -and -not $SkipBuild) {
  Step "Building images with Cloud Build (no local Docker needed)"
  Push-Location $root
  try {
    Info "worker ..."
    $r = G builds submit --config worker/cloudbuild.yaml --substitutions="_REGION=$Region" .
    if (-not $r.Ok) { throw "worker build failed:`n$($r.Out)" }
    Ok "worker image pushed"

    Info "api ..."
    $r = G builds submit --tag "$registry/api:latest" .
    if (-not $r.Ok) { throw "api build failed:`n$($r.Out)" }
    Ok "api image pushed"
  }
  finally { Pop-Location }
}

# ------------------------------------------------------------------ worker

$appId = ""
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern "^GITHUB_APP_ID=(.+)$"
  if ($m) { $appId = $m.Matches[0].Groups[1].Value.Trim() }
}

if (Should "worker") {
  Step "Deploying the worker (private)"
  $r = G run deploy a11y-worker `
    --image "$registry/worker:latest" --region $Region --service-account $saWorker `
    --no-allow-unauthenticated --memory 2Gi --cpu 2 --concurrency 2 --timeout 600 `
    --min-instances 0 --max-instances 5 `
    --set-env-vars "GITHUB_APP_ID=$appId,GCP_PROJECT_ID=$Project,GEMINI_MODEL=gemini-3.1-flash-lite,MAPPER_MAX_FINDINGS=10" `
    --set-secrets "GITHUB_PRIVATE_KEY=github-private-key:latest,GITHUB_WEBHOOK_SECRET=github-webhook-secret:latest,APP_ENCRYPTION_KEY=app-encryption-key:latest,GEMINI_API_KEY=gemini-api-key:latest" `
    --quiet
  if (-not $r.Ok) { throw "worker deploy failed:`n$($r.Out)" }

  $null = G run services add-iam-policy-binding a11y-worker --region $Region `
    --member="serviceAccount:$saTasks" --role="roles/run.invoker"
  Ok "deployed, and only Cloud Tasks may invoke it"
}

$workerUrl = (G run services describe a11y-worker --region $Region --format="value(status.url)").Out
if ($workerUrl) { Info "worker: $workerUrl" }

# --------------------------------------------------------------------- api

if (Should "api") {
  Step "Deploying the api (public)"
  if (-not $workerUrl) { throw "worker URL unknown; deploy the worker first" }
  # --no-cpu-throttling is load-bearing: the webhook returns 202 and then
  # creates the check run in a background task. Throttled, that task freezes.
  $r = G run deploy a11y-api `
    --image "$registry/api:latest" --region $Region --service-account $saApi `
    --allow-unauthenticated --no-cpu-throttling `
    --memory 512Mi --cpu 1 --concurrency 40 --timeout 60 --min-instances 0 --max-instances 3 `
    --set-env-vars "GITHUB_APP_ID=$appId,GCP_PROJECT_ID=$Project,ENABLE_CLOUD_TASKS=true,TASKS_LOCATION=$Region,TASKS_QUEUE=a11y-scans,WORKER_BASE_URL=$workerUrl,TASKS_INVOKER_SA=$saTasks,PREVIEW_WAIT_MINUTES=15,DASHBOARD_ORIGINS=https://$Domain" `
    --set-secrets "GITHUB_PRIVATE_KEY=github-private-key:latest,GITHUB_WEBHOOK_SECRET=github-webhook-secret:latest,APP_ENCRYPTION_KEY=app-encryption-key:latest" `
    --quiet
  if (-not $r.Ok) { throw "api deploy failed:`n$($r.Out)" }
  Ok "deployed with CPU always allocated"
}

$apiUrl = (G run services describe a11y-api --region $Region --format="value(status.url)").Out
if ($apiUrl) {
  Info "api: $apiUrl"
  try {
    $health = Invoke-RestMethod -Uri "$apiUrl/healthz" -TimeoutSec 30
    Ok "healthz: $($health.status)"
  }
  catch { Warn "healthz did not answer yet: $($_.Exception.Message)" }
}

# ---------------------------------------------------------------- firebase

if (Should "firebase") {
  Step "Firestore rules and indexes"
  Push-Location $root
  try {
    # Deploy before the dashboard: without the composite indexes every scan
    # query fails with a missing-index error.
    & $firebase use $Project --non-interactive 2>&1 | Out-Null
    & $firebase deploy --only firestore:rules,firestore:indexes --project $Project --non-interactive
    if ($LASTEXITCODE -ne 0) { Warn "firebase deploy failed -- run '$firebase login' once, then re-run with -Only firebase" }
    else { Ok "rules and indexes deployed" }
  }
  finally { Pop-Location }

  $null = G firestore fields ttls update expires_at --collection-group=deliveries --enable-ttl --quiet
  Ok "TTL set on deliveries"
}

# --------------------------------------------------------------------- web

if (Should "web") {
  Step "Dashboard (Vercel)"
  Push-Location (Join-Path $root "web")
  try {
    if (-not (Test-Path ".vercel\project.json")) {
      Warn "not linked yet. Run once, interactively:  $vercel link"
    }
    else {
      & $vercel --prod --yes
      if ($LASTEXITCODE -ne 0) { Warn "vercel deploy failed" } else { Ok "deployed" }
    }
  }
  finally { Pop-Location }
}

# -------------------------------------------------------------------- cors

if ((Should "cors") -and $apiUrl) {
  Step "Closing the loop: dashboard origin on the api"
  $origins = "https://$Domain"
  $null = G run services update a11y-api --region $Region --update-env-vars "DASHBOARD_ORIGINS=$origins" --quiet
  Ok "DASHBOARD_ORIGINS=$origins"
}

# ------------------------------------------------------------------ report

Step "Done"
Info "api     $apiUrl"
Info "worker  $workerUrl"
Info "webhook $apiUrl/webhooks/github"
Write-Host ""
Write-Host "    Next:" -ForegroundColor White
Write-Host "      1. Point $Domain at the Vercel deployment." -ForegroundColor White
Write-Host "      2. Firebase console -> Authentication -> enable GitHub, paste the" -ForegroundColor White
Write-Host "         OAuth client id and secret, then add $Domain to Authorized domains." -ForegroundColor White
Write-Host "      3. Verify end to end:" -ForegroundColor White
Write-Host '           $env:GH_TOKEN = gh auth token' -ForegroundColor White
Write-Host '           .venv\Scripts\python.exe scripts\smoke_test.py --repo OWNER/REPO' -ForegroundColor White
