#!/usr/bin/env bash
# Restart the RAGFlow Docker Compose deployment for this workspace.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${SCRIPT_DIR}/docker"
ENV_FILE="${COMPOSE_DIR}/.env.knowledge-dev"
OVERLAY_FILE="${COMPOSE_DIR}/docker-compose.knowledge-dev.yml"
PULL_IMAGES=false

usage() {
    cat <<'EOF'
Usage: ./re_start.sh [--pull]

Recreates the established RAGFlow knowledge-base development deployment while
preserving existing data volumes. It uses the GPU Elasticsearch configuration
from docker/.env.knowledge-dev.

Options:
  --pull       Pull configured images before restarting services
  -h, --help   Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --pull)
            PULL_IMAGES=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [ ! -f "${COMPOSE_DIR}/docker-compose.yml" ] || [ ! -f "${ENV_FILE}" ] || [ ! -f "${OVERLAY_FILE}" ]; then
    echo "Error: RAGFlow knowledge-base Compose files were not found in ${COMPOSE_DIR}." >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: Docker is not installed or is not available in PATH." >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Error: Docker Compose v2 is not available. Install the Docker Compose plugin and retry." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "Error: Docker daemon is unavailable. Start Docker and rerun this script." >&2
    exit 1
fi

cd "${COMPOSE_DIR}"
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f docker-compose.yml -f "${OVERLAY_FILE}" --profile elasticsearch --profile gpu)

echo "Validating RAGFlow Compose configuration..."
"${COMPOSE[@]}" config -q

if [ "${PULL_IMAGES}" = true ]; then
    echo "Pulling configured RAGFlow images..."
    "${COMPOSE[@]}" pull
fi

echo "Deploying RAGFlow from ${SCRIPT_DIR}..."
"${COMPOSE[@]}" up -d --force-recreate

echo
echo "RAGFlow service status:"
"${COMPOSE[@]}" ps

# The compose overlay mounts the checkout file-by-file, so a recreate can
# silently come back with stale bytes. Verify rather than assume.
RAGFLOW_CONTAINER="$("${COMPOSE[@]}" ps -q ragflow-gpu 2>/dev/null | head -1)"
if [ -n "${RAGFLOW_CONTAINER}" ]; then
    echo
    echo "Verifying the container picked up the mounted sources..."
    if ! "${SCRIPT_DIR}/check_source_drift.sh" "${RAGFLOW_CONTAINER}"; then
        echo "The container is up but is not running the current checkout." >&2
        exit 1
    fi
fi
