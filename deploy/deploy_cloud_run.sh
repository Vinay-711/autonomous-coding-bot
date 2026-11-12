#!/usr/bin/env bash
# ==============================================================================
# AutoCoder AI - Cloud Run Jobs & Cloud Scheduler Deployment Script
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Configuration
PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
REGION="${GCP_REGION:-asia-south1}"  # Default to Mumbai, India (Asia/Kolkata proximity)
JOB_NAME="autocoder-daily-worker"
SCHEDULER_JOB_NAME="autocoder-daily-trigger"
IMAGE_TAG="gcr.io/${PROJECT_ID}/autocoder-cloud-worker:latest"
RUNNER_SA="autocoder-runner-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="autocoder-scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# Parse CLI flags
DRY_RUN=false
for arg in "$@"; do
    case "${arg}" in
        --dry-run)
            DRY_RUN=true
            ;;
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
    echo "Usage: ./deploy/deploy_cloud_run.sh --project=YOUR_PROJECT_ID [--region=asia-south1] [--dry-run]"
    exit 1
fi

echo "=============================================================================="
echo "🚀 AutoCoder AI: Deploying Cloud-Native Scheduled Batch Worker"
echo "   GCP Project : ${PROJECT_ID}"
echo "   Region      : ${REGION}"
echo "   Job Name    : ${JOB_NAME}"
echo "   Schedule    : 0 18 * * * (18:00 Asia/Kolkata)"
echo "   Dry Run     : ${DRY_RUN}"
echo "=============================================================================="

if [[ "${DRY_RUN}" == "true" ]]; then
    echo "🔎 [DRY-RUN MODE] Validating configurations without creating cloud resources or incurring costs."
    echo "✅ Project ID: ${PROJECT_ID}"
    echo "✅ Target image: ${IMAGE_TAG}"
    echo "✅ Cloud Scheduler target: https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
    echo "✅ Dry-run check completed successfully."
    exit 0
fi

# 1. Enable required GCP service APIs
echo "📦 Step 1: Enabling required Google Cloud APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    secretmanager.googleapis.com \
    cloudbuild.googleapis.com \
    logging.googleapis.com \
    --project="${PROJECT_ID}"

# 2. Service Accounts & IAM Setup
echo "🔑 Step 2: Configuring Service Accounts and IAM permissions..."
if ! gcloud iam service-accounts describe "${RUNNER_SA}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud iam service-accounts create autocoder-runner-sa \
        --display-name="AutoCoder Cloud Run Worker SA" \
        --project="${PROJECT_ID}"
fi

if ! gcloud iam service-accounts describe "${SCHEDULER_SA}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud iam service-accounts create autocoder-scheduler-sa \
        --display-name="AutoCoder Cloud Scheduler Trigger SA" \
        --project="${PROJECT_ID}"
fi

# Grant Secret Manager Secret Accessor to runner SA
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${RUNNER_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --condition=None

# Grant Cloud Run Invoker to scheduler SA
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SCHEDULER_SA}" \
    --role="roles/run.invoker" \
    --condition=None

# 3. Build & push container image
echo "🐳 Step 3: Building container image via Google Cloud Build..."
gcloud builds submit "${ROOT_DIR}" \
    --tag="${IMAGE_TAG}" \
    --project="${PROJECT_ID}"

# 4. Create or update Cloud Run Job
echo "☁️ Step 4: Deploying Cloud Run Job..."
envsubst < "${SCRIPT_DIR}/cloud-run-job.yaml" | gcloud run jobs replace - \
    --region="${REGION}" \
    --project="${PROJECT_ID}"

# 5. Create or update Cloud Scheduler Job
echo "⏰ Step 5: Configuring Cloud Scheduler (18:00 Asia/Kolkata)..."
SCHEDULER_TARGET_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

if gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" --location="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud scheduler jobs update http "${SCHEDULER_JOB_NAME}" \
        --location="${REGION}" \
        --schedule="0 18 * * *" \
        --time-zone="Asia/Kolkata" \
        --uri="${SCHEDULER_TARGET_URI}" \
        --http-method=POST \
        --oidc-service-account-email="${SCHEDULER_SA}" \
        --oidc-token-audience="https://${REGION}-run.googleapis.com/" \
        --project="${PROJECT_ID}"
else
    gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
        --location="${REGION}" \
        --schedule="0 18 * * *" \
        --time-zone="Asia/Kolkata" \
        --uri="${SCHEDULER_TARGET_URI}" \
        --http-method=POST \
        --oidc-service-account-email="${SCHEDULER_SA}" \
        --oidc-token-audience="https://${REGION}-run.googleapis.com/" \
        --project="${PROJECT_ID}"
fi

echo "=============================================================================="
echo "🎉 DEPLOYMENT COMPLETE!"
echo "   Cloud Run Job     : ${JOB_NAME}"
echo "   Cloud Scheduler   : ${SCHEDULER_JOB_NAME} (0 18 * * * Asia/Kolkata)"
echo "   Trigger manually  : gcloud run jobs execute ${JOB_NAME} --region=${REGION} --project=${PROJECT_ID}"
echo "=============================================================================="

