#!/bin/bash
# Deploy MX Swarm to Google Cloud Platform
# Requires: gcloud CLI authenticated, project configured

set -euo pipefail

# Configuration
PROJECT_ID="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_ACCOUNT="${MX_SERVICE_ACCOUNT:-mx-swarm@${PROJECT_ID}.iam.gserviceaccount.com}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[MX]${NC} $1"; }
warn() { echo -e "${YELLOW}[MX]${NC} $1"; }
error() { echo -e "${RED}[MX]${NC} $1" >&2; }

# Ensure we're in the right directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}/../.."

log "Deploying MX Swarm to GCP"
log "Project: ${PROJECT_ID}"
log "Region: ${REGION}"

# Enable required APIs
log "Enabling required APIs..."
gcloud services enable \
    run.googleapis.com \
    pubsub.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    --project="${PROJECT_ID}"

# Create Artifact Registry repository
log "Creating Artifact Registry repository..."
gcloud artifacts repositories create mx-swarm \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    2>/dev/null || warn "Repository already exists"

# Configure Docker for Artifact Registry
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# Build and push images
REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/mx-swarm"

log "Building worker image..."
docker build -f Dockerfile.worker -t "${REGISTRY}/worker:latest" .
docker push "${REGISTRY}/worker:latest"

log "Building planner image..."
docker build -t "${REGISTRY}/planner:latest" .
docker push "${REGISTRY}/planner:latest"

# Create Pub/Sub topic and subscription
log "Creating Pub/Sub resources..."
gcloud pubsub topics create mx-tasks --project="${PROJECT_ID}" 2>/dev/null || warn "Topic already exists"
gcloud pubsub subscriptions create mx-worker-sub \
    --topic=mx-tasks \
    --ack-deadline=300 \
    --project="${PROJECT_ID}" 2>/dev/null || warn "Subscription already exists"

# Create secrets (if they don't exist)
log "Setting up secrets..."
if ! gcloud secrets describe anthropic-api-key --project="${PROJECT_ID}" 2>/dev/null; then
    warn "Create ANTHROPIC_API_KEY secret:"
    echo "gcloud secrets create anthropic-api-key --project=${PROJECT_ID}"
    echo "echo -n 'your-api-key' | gcloud secrets versions add anthropic-api-key --data-file=- --project=${PROJECT_ID}"
fi

if ! gcloud secrets describe github-token --project="${PROJECT_ID}" 2>/dev/null; then
    warn "Create GITHUB_TOKEN secret:"
    echo "gcloud secrets create github-token --project=${PROJECT_ID}"
    echo "echo -n 'your-token' | gcloud secrets versions add github-token --data-file=- --project=${PROJECT_ID}"
fi

# Deploy worker to Cloud Run
log "Deploying worker to Cloud Run..."
gcloud run deploy mx-worker \
    --image="${REGISTRY}/worker:latest" \
    --region="${REGION}" \
    --platform=managed \
    --memory=1Gi \
    --cpu=1 \
    --min-instances=0 \
    --max-instances=100 \
    --timeout=3600 \
    --set-env-vars="MX_QUEUE_PROVIDER=gcp,GCP_PROJECT_ID=${PROJECT_ID}" \
    --set-secrets="ANTHROPIC_API_KEY=anthropic-api-key:latest,GITHUB_TOKEN=github-token:latest" \
    --service-account="${SERVICE_ACCOUNT}" \
    --no-allow-unauthenticated \
    --project="${PROJECT_ID}"

# Deploy planner as a job (one-time execution)
log "Creating planner Cloud Run job..."
gcloud run jobs create mx-planner \
    --image="${REGISTRY}/planner:latest" \
    --region="${REGION}" \
    --memory=2Gi \
    --cpu=2 \
    --task-timeout=3600 \
    --max-retries=1 \
    --set-env-vars="MX_QUEUE_PROVIDER=gcp,GCP_PROJECT_ID=${PROJECT_ID}" \
    --set-secrets="ANTHROPIC_API_KEY=anthropic-api-key:latest,GITHUB_TOKEN=github-token:latest" \
    --service-account="${SERVICE_ACCOUNT}" \
    --project="${PROJECT_ID}" 2>/dev/null || \
gcloud run jobs update mx-planner \
    --image="${REGISTRY}/planner:latest" \
    --region="${REGION}" \
    --project="${PROJECT_ID}"

log "Deployment complete!"
echo ""
echo "To start the swarm:"
echo "  1. Run planner: gcloud run jobs execute mx-planner --region=${REGION} --project=${PROJECT_ID}"
echo "  2. Workers will auto-scale based on queue depth"
echo ""
echo "To monitor:"
echo "  - Cloud Run: https://console.cloud.google.com/run?project=${PROJECT_ID}"
echo "  - Pub/Sub: https://console.cloud.google.com/cloudpubsub/topic/detail/mx-tasks?project=${PROJECT_ID}"
