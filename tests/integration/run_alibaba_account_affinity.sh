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

# MODEL_ROUTING_CONFIG_URL se fija a una URL inalcanzable a proposito (29-09): el
# runner arc-k8s vive DENTRO del cluster, y sin esto el contenedor resuelve el
# default de session_router (dgx-dashboard-backend.control-nexus.svc.../api/
# model-routing/config) y el test leeria los pesos REALES del panel. Esos pesos
# fluctuan durante la corrida (el freno los mueve con el consumo, SWR de 5 s) y
# cuando bajo carga a un pod le expira el timeout del GET de pin, la sesion se
# re-hash contra los pesos nuevos y cambia de cuenta -> "N sesiones mezclaron
# cuenta". Rojo en CI desde #177, verde en host (donde el DNS del cluster no
# existe). Reproducido en local contra un servidor de pesos fluctuantes: peta
# igual; con este aislamiento pasa. Aqui se prueba la afinidad con el reparto
# determinista uniforme; el camino ponderado lo cubren los unitarios
# (tests/test_alibaba_account_affinity.py).
run() {
  docker run --rm --network "$NET" -v "$REPO:/repo:ro" -v "$OUTDIR:/out" \
    -e REPO=/repo -e OUT=/out/obs.jsonl -e REDIS_URL="redis://$NET-valkey:6379/0" \
    -e MODEL_ROUTING_CONFIG_URL="http://127.0.0.1:1/api/model-routing/config" \
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
