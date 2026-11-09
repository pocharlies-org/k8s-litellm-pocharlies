# Evidencia de tests — INFRA-1081 (rama INFRA-1081-fallback-motivo)

Dónde: worktree `/home/dibanez/k8s/k8s-litellm-pocharlies/.claude/worktrees/INFRA-1081` (x86, pytest del repo, sin cluster).

## Contrato nuevo (el sello y el enum)

```
$ python3 -m pytest tests/test_fallback_motivo_stamp_contract.py -q
....................................                                     [100%]
36 passed in 2.17s
```

## Forma del ConfigMap (rev bump + grafo de fallbacks intacto)

```
$ python3 -m pytest tests/test_configmap_revision_bump_contract.py tests/test_tooling_profile_contract.py -q
.............                                                            [100%]
13 passed in 4.80s
```

El diff del manifiesto renderizado es esta PR: dos grupos nuevos en `model_list`
(`infra1081-fallback-probe`/`infra1081-fallback-mock`), el bloque del enum y el
sello en el hook, y el bump `config-a0d30be451d1 → config-2a202e15edb5` (las dos
anotaciones), validado por el contrato de revisión.

## Suite completa

```
$ python3 -m pytest tests/ -q --ignore=tests/integration
1114 passed, 2 skipped in 138.11s (0:02:18)
```

## Criterios

- C1-fila (mecanismo): los cuatro campos se sellan DENTRO de `spend_logs_metadata`
  (test del sello anidado, prioridad del lector, nunca clave plana); la ráfaga en
  vivo se mide tras el merge, antes de fusionar la PR de dgx-infra (orden del
  spec) — captura en 50-entrega.md de INFRA-1081.
- Rollout seguro: prueba de forma del ConfigMap en verde (arriba); el sello y la
  clasificación envueltos en try/except (patrón `_stamp_claude_class`); validación
  en el pod vivo tras el merge, en 50-entrega.md.
- C4-no rompe: esta PR no toca el dashboard; la captura antes/después de las
  claves de `/api/inference/traffic*` va en la PR de dgx-infra y en 50-entrega.md.
