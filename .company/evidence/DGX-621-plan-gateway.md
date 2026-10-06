# Evidencia DGX-621-plan-gateway

Rol: developer · Fecha: 2026-10-06 · Estado: PASA

## Suite completa (venv limpio py3.12, `pip install --require-hashes -r .github/requirements/litellm-contracts.txt`)

Comando: `python -m pytest tests/ -q -p no:cacheprovider` en el worktree `maker-developer-dgx-621` (head 8d3b808).
Resultado: PASA

```
842 passed in 115.88s (0:01:55)
```

Tests del gateway solos: `python -m pytest tests/test_plan_gateway_contract.py -q` -> `38 passed`.

## Integración `bash tests/integration/run_alibaba_account_affinity.sh` (docker, imagen pineada del proxy)

Resultado: PASA (exit code 0)

```
imagen: ghcr.io/berriai/litellm:v1.100.0@sha256:c8756e7b9a61fe45df2ccb5b781d388c3b2f3a21ef9e4956630caef20f9f03aa
plan_gateway: import y ASGI ok en 3.13.15
mix pod-b: ok
mix pod-a: ok
session_router: sesion ses-integracion-mudanza muda de cuenta Alibaba k1 -> k2 (alibaba-q38-max sin deployment sano)
move pod-a: ok
after pod-b: ok
mix pod-b: ok
{"requests": 4543, "sessions": 241, "groups": 10, "pods": ["pod-a", "pod-b"], "sessions_per_account": {"k1": 116, "k2": 125}, "mixed": []}
```

## Criterios

- Forma Deployment/Service/PDB, imagen, command, mounts, ExternalSecret: `tests/test_plan_gateway_contract.py` (5 pruebas de forma).
- C2: `test_c2_el_gateway_no_define_formula_de_eleccion`, `test_c2_session_router_y_gateway_se_importan_sin_litellm`.
- C4: ocho escenarios `test_c4_*` con `httpx.MockTransport` sobre el ASGI.
- C5: `test_c5_ledger_una_linea_json_por_intento_con_todos_los_campos`.
- Allowlist, auth, headers, 413, semáforo, tareas, lifespan: resto de `tests/test_plan_gateway_contract.py`.
- Bump de anotaciones (3 parejas): `tests/test_configmap_revision_bump_contract.py`. Copia dev/embed: `tests/test_session_router_dev_copy_contract.py`.
- Import `httpx, uvicorn, fastapi` y del gateway dentro de la imagen pineada: primeras líneas de la integración.
