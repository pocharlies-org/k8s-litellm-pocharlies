# Evidencia SC-6436 — SC-6436-alias-cleanup

- Comando: `python3 -m pytest tests/ -q` (ejecutado en el worktree, host x86 del runner-equivalente del CI, que corre `python -m pytest tests/ -q`)
- Resultado: PASS — 1218 passed, 3 skipped en 168.37s (dos fallos intermedios causados por la propia migración — el alias en mayúsculas del test de saneado y la anotación de revisión del ConfigMap — arreglados con el `--fix` del contrato; tras ello, suite completa verde).

```
1218 passed, 3 skipped in 168.37s (0:02:48)
```

## Criterios

- C1: `git grep -n "claude-x86,hermes,hermes-batch,synapse" -- k8s/manifest.yaml` → vacío; `git grep -n "claude-atlantic-compania,hermes,hermes-batch,synapse" -- k8s/manifest.yaml` → 1 coincidencia (línea del default del hook).
- C2: `git grep -l "claude-x86" -- tests/` → vacío (antes: 11 ficheros); suite verde arriba.
- C3: `git grep -n "claude-x86" -- ARCHITECTURE.md` → vacío.
- C4: no aplica a esta PR (SC-6494, PR del 2026-12-02, nunca antes del remove_after).
- C5: `dgx-infra` sin ningún cambio en esta rama (el diff solo toca k8s-litellm-pocharlies); la justificación va en la descripción de la PR.
