# Evidencia DGX-721 — sello `refusal_score` en producción (P1 de DGX-391)

Rol: developer · Fecha: 2026-11-15 · Sesión: 565958c6-a59b-46b3-9246-b6f5a10d30cb · Estado: LISTO

Esta parte no añade código: el contrato `dgx.litellm.refusal-probe-stamp.v1`
(`CONTRACTS.yaml`, `status: active`) y su test hereético
(`tests/test_refusal_probe_stamp_contract.py`) ya fijan el sello. Aquí va la
evidencia de los tres criterios del `00-spec.md`, ejecutada en el worktree
`/home/dibanez/k8s/k8s-litellm-pocharlies/.claude/worktrees/DGX-721-refusal-stamp`
(rama `DGX-721-refusal-stamp-evidence`, base `origin/main` 749bd38).

## C1 — El test del contrato del sello está en verde: PASS

Comando (desde el worktree, mismo test que fija el contrato, sin reescribir):

```
python3 -m pytest -q tests/test_refusal_probe_stamp_contract.py
```

Salida real (2026-11-15):

```
......................                                                   [100%]
22 passed in 0.93s
```

22 passed, ninguna línea con `failed` ni `error`. (pytest imprime además un
`PytestDeprecationWarning` de `pytest-asyncio` ajeno a este test.)

## C2 — ≥ 99 % de peticiones 24 h con `refusal_score`: NO MEDIDO (permiso denegado al rol)

Pod primario confirmado (el del spec, `postgres-shared-1`, no existe):

```
$ kubectl get clusters.postgresql.cnpg.io -n databases
NAME                   AGE   INSTANCES   READY   STATUS                     PRIMARY
postgres-shared-nvme   22d   2           2       Cluster in healthy state   postgres-shared-nvme-1
```

La consulta del spec sobre ese primario fue DENEGADA por el mapa de permisos
del rol developer (`kubectl exec` fuera de su mapa; no se busca la variante
que la salte). Comando exacto pendiente de quien tenga lectura de la BD
(devops/operador):

```
kubectl exec -n databases postgres-shared-nvme-1 -- psql -U postgres -d litellm -At -c "SELECT round(100.0*count(*) FILTER (WHERE jsonb_extract_path(metadata::jsonb, 'spend_logs_metadata', 'refusal_score') IS NOT NULL)/count(*),2) FROM \"LiteLLM_SpendLogs\" WHERE model LIKE '%qwen38-flash-next%' AND \"startTime\" > now() - interval '24 hours';"
```

Último dato conocido (spec, 24-09): 1344 de unas 1350. Las ráfagas con master
key dan 403 por `pre_call_hook` y no cuentan en el denominador (FINDING 30-09).

## C3 — Medir sin alterar (λ=0 vs λ=1): ABIERTO, declarado según el spec

Pre-check obligatorio, lectura única (2026-11-15):

```
$ kubectl get cm -n comfyui gpu-arbiter-state -o jsonpath='{.data.compute_mode}'
{"desired_mode":"llm-tp","effective_mode":"llm-tp","previous_mode":"llm-tp","phase":"ready",...}
```

Con `effective_mode: llm-tp` no cabe crear carga en las Sparks, y no existe
canario del residente en ningún namespace (buscado: `kubectl get deploy,sts -A`
filtrando `canary|canario|refusal` → sin resultados). El spec lo prevé: «Si el
canario no existe todavía, el criterio queda abierto y así se dice en la
validación». No se ha tocado el residente qwen38-flash-next ni
`VLLM_REFUSAL_PROBE`, y no se ha apagado la sonda en producción.

## Resumen por criterio

- C1 (test del contrato en verde): CUMPLE — 22 passed, sin failed/error.
- C2 (≥ 99 % con sello en 24 h): NO MEDIDO — pod primario confirmado
  (`postgres-shared-nvme-1`); la consulta queda pendiente de un rol con lectura
  de la BD (comando exacto arriba).
- C3 (canario λ=0 vs λ=1 → IDENTICO): ABIERTO — sin canario y con el perfil de
  cómputo en `llm-tp`; declarado, no medido, sin alterar nada.
