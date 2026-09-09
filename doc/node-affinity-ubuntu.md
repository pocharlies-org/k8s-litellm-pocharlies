# Por que litellm esta anclado a `ubuntu` (SC-404, 2026-09-09)

El Deployment `litellm` fija `kubernetes.io/hostname In [ubuntu]`. SC-404 pidio
ensancharlo para que las 2 replicas vivieran en nodos distintos. Se comprobo con
los 7 nodos del cluster y **no hay hoy un segundo nodo honesto**: el anclaje se
mantiene como decision consciente, no como accidente. Este documento es el
motivo medido; el pin esta en `k8s/manifest.yaml` (comentario sobre
`tolerations`/`affinity` del Deployment) y lo guarda
`tests/test_litellm_rollout_shape_contract.py::test_the_ubuntu_anchor_is_a_decision_not_an_accidente`.

## Nodos considerados (medido el 2026-09-09)

`kubectl top nodes`, `kubectl describe node` (taints) y el arbiter
(`kubectl get cm -n comfyui gpu-arbiter-state`: `effective_mode: llm-tp`,
`phase: ready`, `blockers: []`).

| nodo | arch | uso medido | taint | veredicto |
|---|---|---|---|---|
| `ubuntu` | amd64 | 40% mem (25.9/62.7 GiB), 61% CPU pedida | `pool=dev:PreferNoSchedule` (blando, tolerado) | **elegido** — unico worker general sin taint duro |
| `nvidia-dgx` | arm64 | 7% mem (8.7/119.6 GiB) | `dedicated=llm:NoSchedule` | descartado (abajo) |
| `gx10-ec3d` | arm64 | 11% mem (14.1/119.5 GiB) | `dedicated=llm:NoSchedule` | descartado (abajo) |
| `ks5-cp-1/2/3` | amd64 | 57% / 39% / 25% mem | ninguno | excluidos por la epica SC-381: plano de control + etcd |
| `sauvage` | amd64 | 21% mem | `role=edge:NoSchedule` (duro, no tolerado) | excluido por la epica: OVH, al otro lado de la WAN de los backends vLLM; y el pod de litellm no tolera ese taint |

Los dos hechos que no son evidentes:

- **La arquitectura no es el motivo.** `ghcr.io/berriai/litellm:v1.100.0` esta
  fijado por digest a un OCI index que incluye `linux/arm64` y `linux/amd64`
  (verificado con `docker manifest inspect`). Un Spark podria correr el binario.
- **El taint es el motivo duro.** `dedicated=llm:NoSchedule` en ambos Sparks y
  el pod solo tolera `pool=dev:PreferNoSchedule`. Con el `nodeAffinity` ensanchado
  pero sin `tolerations`, el scheduler sigue eligiendo `ubuntu`: el PR daria un
  manifiesto que promete reparto y un `kubectl get pods -o wide` identico al de
  hoy. Colocar de verdad exige meter al router en el pool dedicado de GPU.

## Por que `nvidia-dgx` no es el segundo nodo, aun con 7% de memoria

1. **Acoplamiento de fallo.** `nvidia-dgx` es el worker del perfil `llm-tp`
   (`qwen38-flash-next-worker`) y el NFS server de los pesos. Es ademas backend
   del propio router: una caida del nodo tumba a la vez una replica del router y
   un trozo del pool al que enruta. La redundancia que se compra queda
   correlacionada con lo que debe cubrir.
2. **Radio del OOM del 19-08.** Los dos Sparks tienen memoria unificada y el
   scheduler solo ve `requests`. El SystemOOM del 19-08 (Job de ~31 GB con
   `limits.memory: 1Gi`) tumbo `gx10-ec3d` entero y con el vLLM del head y
   LiteLLM sin inferencia. La regla de la casa: nunca programar a ciegas en los
   Sparks. Un router con historial de crecer hasta 6Gi no es el primer inquilino
   que entra por ahi.
3. **Es decision de otro.** El taint `dedicated=llm` lo puso el dueno del pool de
   GPU para reservar los Sparks a cargas de inferencia. Cruzarlo desde un PR de
   litellm es tomar esa decision por él.

## Cuando se levanta el anclaje

- Aparece un **segundo worker general sin taint duro** (amd64 o, ya verificado,
  arm64 vale porque la imagen es multi-arch): se cambia `values: [ubuntu]` por
  los dos hostnames, se pasa el test del anclaje con la evidencia nueva pegada, y
  la maquinaria de reparto (`topologySpreadConstraints` + `maxSurge 1` +
  `ScheduleAnyway`, medida en 134 s) ya esta montada.
- O el owner del pool GPU decide que el router entra en `dedicated=llm`: entonces
  hace falta tambien la toleracion, y re-medir el reparto y la suma de
  `/internal/active-requests` entre nodos (criterios 1-3 de SC-404).

Lo que NO cambia en ninguno de los dos casos: `limits.memory: 6Gi`, las sondas
sobre `/health/liveliness` (SC-294), `replicas: 2`, el drenaje de 660 s (grace period 720 s) y el PDB
con `maxUnavailable: 1`.
