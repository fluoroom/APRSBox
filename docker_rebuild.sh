#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG="aprsbox:local"
CONTAINER_NAME="aprsbox"
HOST_PORT="8085"

echo "[APRSBox] Building image ${IMAGE_TAG} from $(pwd)..."
docker build -t "${IMAGE_TAG}" .

if docker ps -a --format '{{.Names}}' | grep -Fxq "${CONTAINER_NAME}"; then
    echo "[APRSBox] Stopping existing container ${CONTAINER_NAME}..."
    docker stop "${CONTAINER_NAME}" >/dev/null || true
    docker rm "${CONTAINER_NAME}" >/dev/null || true
fi

echo "[APRSBox] Starting ${CONTAINER_NAME} on host port ${HOST_PORT}..."
docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    -p "${HOST_PORT}:8000" \
    --add-host=host.docker.internal:host-gateway \
    -v aprsbox_data:/opt/aprsbox/data \
    -v aprsbox_logs:/opt/aprsbox/logs \
    "${IMAGE_TAG}"

echo "[APRSBox] Up. Web UI: http://localhost:${HOST_PORT}"
echo "[APRSBox] Tail logs with: docker logs -f ${CONTAINER_NAME}"
