#!/usr/bin/env bash
# SC-2917: semantica del semaforo `max_parallel_requests` con el Router REAL de la imagen
# pineada del proxy (N+1 llamadas concurrentes sobre una deployment con tope N: debe encolar).
# Uso: tests/integration/run_semaphore_queue_contract.sh   (necesita docker y la imagen)
set -euo pipefail
cd "$(dirname "$0")/../.."
IMAGE=$(grep -E '^ +image: harbor.lan.e-dani.com/homelab/litellm:' k8s/manifest.yaml | head -1 | awk '{print $2}')
echo "imagen: $IMAGE"
docker run --rm -v "$(pwd):/repo:ro" -e REPO=/repo -e SEM_TEST_IMAGE="$IMAGE" \
  -e LITELLM_LOG=ERROR --entrypoint python "$IMAGE" \
  /repo/tests/integration/semaphore_queue_contract.py
