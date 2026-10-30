# Evidencia · DGX-639 rework (draw peso cero)

Rama `dgx-639-draw-peso-cero` · ejecutadas en el worktree, x86, python 3.12 / pytest 9.0.3 (mismo conjunto que CI).

## Rojo ANTES del arreglo (tests nuevos del defecto)

Comando: `python3 -m pytest tests/test_alibaba_account_affinity.py -k 'cero or lectura or caducada or reintroduce' -q`

```
FAILED tests/test_alibaba_account_affinity.py::test_lectura_de_pesos_que_cae_mantiene_fuera_a_k1_conocida
FAILED tests/test_alibaba_account_affinity.py::test_filtro_sin_sid_con_lectura_rota_mantiene_k1_fuera
FAILED tests/test_alibaba_account_affinity.py::test_sesion_caducada_con_lectura_rota_no_cae_en_k1
FAILED tests/test_alibaba_account_affinity.py::test_fail_open_sin_sid_no_reintroduce_la_cuenta_sin_cupo
4 failed, 4 passed, 45 deselected in 1.14s
```

Los 4 fallos son el defecto medido (pesos conocidos {k1: 0.0, k2: 1.0} + lectura de pesos que cae, y fail-open a la lista completa): `el filtro devolvio varias cuentas: {'k2', 'k1'}` y `k1` salia en ~50 % de los sorteos. Los 4 passed son las regresiones (sorteo ponderado y camino sin sid con pesos leidos, ya correctos).

## Verde DESPUES del arreglo

Comando: `python3 -m pytest tests/test_alibaba_account_affinity.py tests/test_session_router_dev_copy_contract.py tests/test_configmap_revision_bump_contract.py tests/test_plan_gateway_contract.py tests/test_config_stream.py -q`

```
97 passed in 6.87s
```

Suite completa del repo (la de CI, sin `tests/integration`, que necesita la imagen): `python3 -m pytest tests/ -q -x --ignore=tests/integration`

```
1070 passed, 2 skipped in 130.15s (0:02:10)
```

## Criterios (00-spec.md de DGX-639)

- C1: test determinista que reproduce el fallo y falla contra el codigo actual (rojo arriba), verde tras el arreglo — cubierto por esta PR.
- C2: afinidad dentro del TTL intacta — verde (suite `test_alibaba_account_affinity.py` completa).
- C3: determinismo entre replicas — verde (mismos tests, sin regresion).
- C4: reparto sin sid y demas criterios de DGX-620 sin regresion; coherencia dev/ ↔ manifiesto — verde (`test_session_router_dev_copy_contract.py`, suite completa 1070).
- C5: medicion 24 h post-despliegue del CTO — no es de esta PR (medida tras el merge).
