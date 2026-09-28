#!/usr/bin/env bash
# Integracion de la afinidad por cuenta de Alibaba: Router REAL de la imagen
# pineada del proxy + Valkey real + dos procesos como dos pods.
# Uso: tests/integration/run_alibaba_account_affinity.sh   (necesita docker)
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO=$(pwd)
IMAGE=$(grep -E '^ +image: ghcr.io/berriai/litellm:' k8s/manifest.yaml | head -1 | awk '{print $2}')
NET=affinity-it-$$
docker network create "$NET" >/dev/null
trap 'docker rm -f "$NET-valkey" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1' EXIT
docker run -d --rm --name "$NET-valkey" --network "$NET" valkey/valkey:8-alpine >/dev/null
echo "imagen: $IMAGE"
OUTDIR=$(mktemp -d -p "${RUNNER_TEMP:-/tmp}")
chmod 777 "$OUTDIR"

run() {
  docker run --rm --network "$NET" -v "$REPO:/repo:ro" -v "$OUTDIR:/out" \
    -e REPO=/repo -e OUT=/out/obs.jsonl -e REDIS_URL="redis://$NET-valkey:6379/0" \
    -e LITELLM_LOG=ERROR --entrypoint python "$IMAGE" \
    /repo/tests/integration/alibaba_account_affinity_router.py "$@"
}

run mix pod-a & A=$!
run mix pod-b & B=$!
wait $A; wait $B
run move pod-a
run after pod-b
run mix pod-b
run check
