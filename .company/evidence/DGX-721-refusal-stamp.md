# Evidencia DGX-721 — sello `refusal_score` en producción (P1 de DGX-391)

Rol: developer · Fecha: 2026-11-15 · Sesión: 565958c6-a59b-46b3-9246-b6f5a10d30cb · Estado: LISTO

> C2 añadida el 2026-11-15 por `devops` (lectura de la BD, el paso que el rol
> developer no podía dar). C1 y C3 siguen como las escribió el maker.

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

## C2 — ≥ 99 % de peticiones 24 h con `refusal_score`: NO CUMPLE — 26.56 %

Lectura ejecutada por `devops` el 2026-11-15 (el paso que el rol developer no
podía dar: `kubectl exec` fuera de su mapa de permisos). Pod primario
confirmado antes de consultar (el del spec, `postgres-shared-1`, no existe; el
real es `postgres-shared-nvme-1`):

```
$ kubectl get clusters.postgresql.cnpg.io -n databases
NAME                   AGE   INSTANCES   READY   STATUS                     PRIMARY
postgres-shared-nvme   22d   2           2       Cluster in healthy state   postgres-shared-nvme-1
```

Comando exacto y salida real:

```
$ kubectl exec -n databases postgres-shared-nvme-1 -- psql -U postgres -d litellm -At -c "SELECT round(100.0*count(*) FILTER (WHERE jsonb_extract_path(metadata::jsonb, 'spend_logs_metadata', 'refusal_score') IS NOT NULL)/count(*),2) FROM \"LiteLLM_SpendLogs\" WHERE model LIKE '%qwen38-flash-next%' AND \"startTime\" > now() - interval '24 hours';"
26.56
```

Denominador y sellados (misma ventana, lectura inmediata posterior; el tráfico
es vivo y por eso el porcentaje difiere en la segunda cifra):

```
$ kubectl exec -n databases postgres-shared-nvme-1 -- psql -U postgres -d litellm -At -c "SELECT count(*) AS total, count(*) FILTER (WHERE jsonb_extract_path(metadata::jsonb,'spend_logs_metadata','refusal_score') IS NOT NULL) AS con_refusal FROM \"LiteLLM_SpendLogs\" WHERE model LIKE '%qwen38-flash-next%' AND \"startTime\" > now() - interval '24 hours';"
15220|4036
```

**Causa, medida (no hipótesis)**: el sello se escribe solo en la ruta
`acompletion`; la ruta `anthropic_messages` no lo escribe nunca.

```
$ kubectl exec -n databases postgres-shared-nvme-1 -- psql -U postgres -d litellm -At -c "SELECT call_type, count(*) AS total, count(*) FILTER (WHERE jsonb_extract_path(metadata::jsonb,'spend_logs_metadata','refusal_score') IS NOT NULL) AS con_refusal FROM \"LiteLLM_SpendLogs\" WHERE model LIKE '%qwen38-flash-next%' AND \"startTime\" > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC;"
anthropic_messages|11004|0
acompletion|4202|4015
```

`acompletion` sí está sellada (4015 de 4202 = 95.6 %); `anthropic_messages`
aporta 11004 filas sin sello y se lleva el global por debajo del 99 %. No es
un hueco de escrituras ni las ráfagas con master key (FINDING 30-09): es una
ruta de llamada sin el sello. El dato del spec (24-09: 1344 de ~1350)
corresponde a una ventana sin tráfico `anthropic_messages` del residente.

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
- C2 (≥ 99 % con sello en 24 h): NO CUMPLE — 26.56 % (4036 selladas de 15220).
  El sello solo se escribe en `acompletion` (95.6 %); `anthropic_messages`
  (11004 filas) no lo lleva nunca.
- C3 (canario λ=0 vs λ=1 → IDENTICO): ABIERTO — sin canario y con el perfil de
  cómputo en `llm-tp`; declarado, no medido, sin alterar nada.
