# DGX-1147 · evidencia del residente que nunca aparca

Rama `feat/resident-cooldown-zero` → `main`. Corre la suite con el venv py3.12 del lock
(`.github/requirements/litellm-contracts.txt`: pytest 9.0.2, PyYAML, httpx), mismo conjunto que el job
`LiteLLM contracts` de CI.

## 1. Reproducción del bug (tronco, antes del arreglo)

El test nuevo se copia tal cual sobre `origin/main` y se corre contra SU manifiesto:

```
$ git worktree add .claude/worktrees/DGX-1147-trunk origin/main
$ cp tests/test_resident_never_cools_contract.py <trunk>/tests/ && cd <trunk>
$ pytest tests/test_resident_never_cools_contract.py -q
E  AssertionError: qwen38-u-off: sin ['ContentPolicyViolationErrorAllowedFails', 'BadRequestErrorAllowedFails',
   'AuthenticationErrorAllowedFails', 'TimeoutErrorAllowedFails', 'RateLimitErrorAllowedFails',
   'InternalServerErrorAllowedFails', 'ServiceUnavailableErrorAllowedFails', 'BadGatewayErrorAllowedFails',
   'NotFoundErrorAllowedFails']
18 failed, 1 passed in 1.23s
```

Fallan las nueve entradas de la familia en los dos tests (cooldown y política): los cuatro gemelos `-tpN`,
`correo-clasificador`, `tooling`, `tooling-uncensored`, `qwen38-off`, `qwen38-u-off`. Es el estado que produjo
los 15 min de `429 all_deployments_in_cooldown` del 19-10.

## 2. Suite completa con el arreglo

```
$ pytest tests/ -q
........................................................................ [  6%]
... (17 filas) ...
...........ss.                                                           [100%]
1163 passed, 3 skipped in 153.78s (0:02:33)
```

3 skipped: los que piden docker/imagen (corren en el job `Alibaba account affinity (integration)`).

## 3. Lo que dice el manifiesto al parsearlo (no lo que dice el texto)

```
$ python -c "…yaml.safe_load del ConfigMap litellm-config…"
qwen38-flash-next-tp2                cooldown=0 politicas=9 techo_min=1000000000
qwen38-flash-next-tp1                cooldown=0 politicas=9 techo_min=1000000000
correo-clasificador                  cooldown=0 politicas=9 techo_min=1000000000
qwen38-flash-next-uncensored-tp2     cooldown=0 politicas=9 techo_min=1000000000
qwen38-flash-next-uncensored-tp1     cooldown=0 politicas=9 techo_min=1000000000
```

Los cuatro perfiles de pool (`tooling`, `tooling-uncensored`, `qwen38-off`, `qwen38-u-off`) comparten
`model_info: &tooling_pool_info`, que mergea el ancla; los cubre
`test_resident_never_cools_contract.py` por `model_name` (los perfiles no llevan `model_info.id`).

## 4. Contra el Router real de la imagen pineada

`tests/integration/resident_twin_cooldown.py` (job `Alibaba account affinity (integration)`, con docker y la
imagen del proxy): 60 timeouts (408), 60 errores 500 y 60 `RateLimitError` de pre-call con el
`cooldown_time` del router por deployment. Ninguna aparcada. Este es el que cierra el camino 2 (el pre-call
aparca con el cooldown del router, no con el de la deployment).

## 5. Detector de copia

```
$ company-duplicados
duplicados: hay duplicación en el árbol, pero ninguna toca líneas añadidas por esta PR.
**Sin duplicación nueva.**
```

## Criterios

- C1: `qwen38-flash-next` tp2 y tp1 con `cooldown_time: 0` y política de 1e9 en las nueve excepciones — medido
  en el parseo del manifiesto (§3) y en `test_resident_never_cools_contract.py`; ninguna de las 60+60+60
  fallos del Router real las aparcó (§4).
- C2: `qwen38-flash-next-uncensored` tp2 y tp1 — ídem (§3, §4).
- C3: `correo-clasificador` con cooldown 0 y sus overrides de 60 s / `allowed_fails_policy: null` fuera del
  manifiesto — medido en §3 y en `test_correo_clasificador_alias_contract.py`.
- C4: `tooling`, `tooling-uncensored`, `qwen38-off`, `qwen38-u-off` — medido por `model_name` en
  `test_resident_never_cools_contract.py` y `test_tooling_profile_contract.py`.
- C5: `router_settings.cooldown_time` sigue en 300 y ningún deployment no residente cambia de cooldown —
  `git diff origin/main...HEAD -- k8s/manifest.yaml` solo toca la familia residente y los comentarios del
  router; lo aserta `test_tooling_profile_contract.py`.
- C6: la salud sigue siendo la única puerta — el diff no añade ni cambia código de salud:
  `live_backends` y `resident_backend_filter` salen intactos salvo un comentario. No es una medida en
  producción: se comprueba por diff y por los tests de afinidad que pasan igual que en el tronco.
- C7: test de contrato sobre toda la familia — `tests/test_resident_never_cools_contract.py`, 19 casos, rojo
  en el tronco (§1) y verde con el arreglo.
- C8: los tests y `CONTRACTS.yaml` afirman que la familia nunca aparca — `test_tooling_profile_contract.py`,
  `test_correo_clasificador_alias_contract.py`, `test_resident_backend_affinity.py` (docstring),
  `tests/integration/resident_twin_cooldown.py` y la nota DGX-1147 de `CONTRACTS.yaml` (que sustituye a la de
  DGX-1146: los 360 s ya no existen).

## Lo que NO se valida aquí

El reinicio de la head del vLLM (OOMKilled con prompts largos) es otra sesión: este cambio quita el
aparcamiento, no la causa de la caída. Y que el cambio llegue a producción se comprueba al aplicar ArgoCD
la anotación `config.k8s.e-dani.com/revision: config-c6c0927cee83`, no con este fichero.
