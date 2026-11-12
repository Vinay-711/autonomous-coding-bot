# AutoCoder AI: Cloud-Native Scheduled Batch Worker Architecture

This document specifies the deployment architecture, configuration, cost estimates, and operational guidelines for running **AutoCoder AI** on Google Cloud Platform as a scheduled serverless batch worker.

---

## 1. System Architecture

```
                                  [Google Cloud Scheduler]
                                (Cron: 0 18 * * * Asia/Kolkata)
                                              │
                                              ▼ (OIDC Auth: roles/run.invoker)
                                   [Google Cloud Run Job]
                              (autocoder-cloud-worker container)
                                ├── Python 3.11 Runtime
                                └── OpenJDK 17 JDK (javac & java)
                                              │
                   ┌──────────────────────────┼──────────────────────────┐
                   ▼                          ▼                          ▼
         [GCP Secret Manager]       [Cloud SQL PostgreSQL]       [Google Cloud Logging]
         - GEMINI_API_KEY           - problems & attempts        (Structured JSON logs)
         - GITHUB_TOKEN             - daily_runs & locks         - Severity, Component
         - DATABASE_URL             - judge_verdicts             - Redacted credentials
```

### Key Components
1. **Cloud Run Jobs**: Executes the batch container once per day. Scales to zero when not running; zero idle cost.
2. **Cloud Scheduler**: Triggers the Cloud Run Job daily at `18:00 Asia/Kolkata` (`0 18 * * *`).
3. **Secret Manager**: Securely stores API keys and GitHub tokens. Loaded at container startup without hardcoding.
4. **Cloud SQL PostgreSQL (or persistent DB)**: Tracks idempotency, crash recovery, and locks to prevent overlapping runs.
5. **Structured Cloud Logging**: JSON logging compatible with Google Cloud Logging (`severity`, `component`, `message`).
6. **Graceful SIGTERM Handling**: Automatically releases problem leases upon container termination or task timeout.

---

## 2. Configuration & Environment Variables

All settings are environment-driven with sane defaults:

| Variable | Required | Default | Description |
|---|---|---|---|
| `PROJECT_ID` | Yes (in Cloud) | None | Google Cloud Project ID |
| `GEMINI_API_KEY` | Yes (or dry-run) | None | Google Gemini API Key (loaded from Secret Manager) |
| `GITHUB_USERNAME` | Yes (submit mode) | None | GitHub handle for repository publishing |
| `GITHUB_TOKEN` | Yes (submit mode) | None | GitHub Personal Access Token (`repo` scope) |
| `DATABASE_URL` | Optional | SQLite path | PostgreSQL connection URL (`postgresql://user:pass@host:5432/db`) |
| `DATA_DIR` | Optional | `/app/data` | Writable directory for runtime data and solutions |
| `DRY_RUN` | Optional | `false` | Deterministic simulation without real Gemini or judge API calls |
| `OFFLINE_MODE` | Optional | `false` | Runs compilation and local solving without browser/website network |
| `MAX_RUN_DURATION_SECONDS` | Optional | `3300` | Maximum container execution duration before graceful shutdown (default 55m) |
| `STRUCTURED_LOGGING` | Optional | `true` (in cloud) | Outputs structured JSON log lines for Google Cloud Logging |

---

## 3. Estimated Cost Considerations

Cloud Run Jobs billing is strictly pay-per-use based on vCPU-seconds and GiB-seconds consumed during execution.

### Monthly Resource Usage (Daily 30-minute run)
- **Execution cadence**: 1 run / day = 30 runs / month.
- **Duration per run**: ~30 minutes (1,800 seconds).
- **Allocated resources**: 2.0 vCPU, 2.0 GiB RAM.
- **vCPU consumption**: `30 runs * 1,800s * 2 vCPU = 108,000 vCPU-seconds / month`.
- **Memory consumption**: `30 runs * 1,800s * 2 GiB = 108,000 GiB-seconds / month`.

### GCP Free Tier Coverage
- **Cloud Run Free Tier**: Includes **180,000 vCPU-seconds** and **360,000 GiB-seconds** per month for free.
- **Cloud Scheduler Free Tier**: Includes **3 free jobs per billing account**.
- **Secret Manager Free Tier**: Includes **6 active secret versions** and **10,000 API operations per month** for free.
- **Cloud Logging Free Tier**: Includes **50 GiB log ingestion** per month for free.

**Estimated Monthly GCP Compute Cost**: **$0.00 / month** (fits entirely inside the Google Cloud Free Tier).
*(If using a dedicated Cloud SQL instance rather than an external or serverless DB, Cloud SQL costs apply separately. Cloud SQL can be substituted with Cloud Storage SQLite backup or serverless PostgreSQL like Neon/Supabase to remain at $0).*

---

## 4. Local Container Testing

### Build Container
```bash
docker build -t autocoder-cloud-worker:latest .
```

### Run Offline Dry-Run in Docker
```bash
docker run --rm \
  -e DRY_RUN=true \
  -e OFFLINE_MODE=true \
  autocoder-cloud-worker:latest \
  --platform leetcode \
  --count 1 \
  --mode run \
  --dry-run
```

---

## 5. Deployment & Teardown

> **IMPORTANT**: In accordance with project instructions, no deployment commands will be run without explicit user confirmation.

### Deploy to GCP
```bash
# Validates configuration without deploying
./deploy/deploy_cloud_run.sh --project=YOUR_PROJECT_ID --dry-run

# Deploys Cloud Run Job and Cloud Scheduler
./deploy/deploy_cloud_run.sh --project=YOUR_PROJECT_ID --region=asia-south1
```

### Teardown Resources
```bash
./deploy/teardown_cloud_run.sh --project=YOUR_PROJECT_ID --region=asia-south1
```

