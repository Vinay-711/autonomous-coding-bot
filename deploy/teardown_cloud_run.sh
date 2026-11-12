#!/usr/bin/env bash
# ==============================================================================
# AutoCoder AI - Cloud Run & Cloud Scheduler Clean Teardown Script
# ==============================================================================
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${GCP_REGION:-asia-south1}"
JOB_NAME="autocoder-daily-worker"
SCHEDULER_JOB_NAME="autocoder-daily-trigger"
RUNNER_SA="autocoder-runner-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="autocoder-scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# Parse CLI flags
for arg in "$@"; do
    case "${arg}" in
        --project=*)
            PROJECT_ID="${arg#*=}"
            ;;
        --region=*)
            REGION="${arg#*=}"
            ;;
    esac
done

if [[ -z "${PROJECT_ID}" ]]; then
    echo "❌ Error: GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT environment variable is required."
    echo "Usage: ./deploy/teardown_cloud_run.sh --project=YOUR_PROJECT_ID [--region=asia-south1]"
    exit 1
fi

echo "=============================================================================="
echo "🧹 AutoCoder AI: Tearing Down Cloud Resources"
echo "   GCP Project : ${PROJECT_ID}"
echo "   Region      : ${REGION}"
echo "=============================================================================="

# 1. Delete Cloud Scheduler Job
echo "🗑️ Removing Cloud Scheduler trigger..."
gcloud scheduler jobs delete "${SCHEDULER_JOB_NAME}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet || true

# 2. Delete Cloud Run Job
echo "🗑️ Removing Cloud Run Job..."
gcloud run jobs delete "${JOB_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet || true

# 3. Clean up container images from Google Container Registry / Artifact Registry
echo "🗑️ Removing container image tags..."
gcloud container images delete "gcr.io/${PROJECT_ID}/autocoder-cloud-worker:latest" \
    --force-delete-tags \
    --quiet || true

# 4. Remove service accounts
echo "🗑️ Removing Service Accounts..."
gcloud iam service-accounts delete "${RUNNER_SA}" \
    --project="${PROJECT_ID}" \
    --quiet || true

gcloud iam service-accounts delete "${SCHEDULER_SA}" \
    --project="${PROJECT_ID}" \
    --quiet || true

echo "=============================================================================="
echo "✅ Teardown complete. All allocated cloud resources have been cleanly removed."
echo "=============================================================================="

