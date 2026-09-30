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
trap 'docker rm -f "$NET-valkey" "$NET-cfg" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1' EXIT
docker run -d --rm --name "$NET-valkey" --network "$NET" valkey/valkey:8-alpine >/dev/null
# Config de routing ESTATICA servida por el propio harness: sin esto el contenedor,
# corriendo dentro del runner ARC (que esta DENTRO del cluster), resuelve el
# dashboard de PROD y lee alibaba_account_weights VIVOS, que el panel recalcula con
# el cupo consumido y cambian cada pocos segundos. Los dos pods cachean instantaneas
# SWR distintas y preferred_account deja de coincidir entre ellos: rojo en CI el
# 29-09 (run 36583219046, 46 sesiones mezcladas con k1/k2). Un test de CI no debe
# hablar con el panel de produccion, y la determinancia entre pods que este test
# mide exige pesos fijos. Con 1.0/1.0 se ejercita ADEMAS la rama ponderada
# (sanitize -> _account_weights -> tramos) con el reparto 50/50 que el test ya
# conadia; el reparto sesgado lo cubren los unitarios (ver static_routing_config.py).
docker run -d --rm --name "$NET-cfg" --network "$NET" -v "$REPO:/repo:ro" \
  --entrypoint python "$IMAGE" /repo/tests/integration/static_routing_config.py >/dev/null
echo "imagen: $IMAGE"
OUTDIR=$(mktemp -d -p "${RUNNER_TEMP:-/tmp}")
chmod 777 "$OUTDIR"

run() {
  docker run --rm --network "$NET" -v "$REPO:/repo:ro" -v "$OUTDIR:/out" \
    -e REPO=/repo -e OUT=/out/obs.jsonl -e REDIS_URL="redis://$NET-valkey:6379/0" \
    -e MODEL_ROUTING_CONFIG_URL="http://$NET-cfg:9002/api/model-routing/config" \
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
