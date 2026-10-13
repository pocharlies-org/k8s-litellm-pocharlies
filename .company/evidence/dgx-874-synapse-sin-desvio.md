# DGX-874 · C1 de security · evidencia

Rol: devops · Fecha: 2026-10-13 · Estado: LISTO

Donde: x86 (`ubuntu`), worktree de `dgx-874-synapse-sin-desvio` cortado de `origin/main` d675bf7; python 3.12.3, pytest 9.0.3.

## 0. Lectura del codigo: una peticion sellada no pasa por el session router

Hook `litellm_strip_params.py` y `session_router.py` del ConfigMap `litellm-config` (`k8s/manifest.yaml`). Es lectura, no medida en vivo.

- `async_pre_call_hook` estampa el sello con `_apply_key_fallback_policy(data, user_api_key_dict)` y solo despues llama a `session_router.apply_session_routing(...)` (lo fija `test_el_sello_se_estampa_antes_del_session_router`).
- `session_router._apply`: tras el filtro `model not in ROUTED_MODELS` hay un `if data.get("disable_fallbacks") is True: ... return False` («mandato 4a»), antes de leer plan, valvula, sticky, KV o re-bind. `test_sellado_gana_a_todo` (ya existia) lo fija con todo encendido.
- `correo-clasificador` no esta en `ROUTED_MODELS` (`qwen38-flash-next` y `alibaba-q38-flash`): `_apply` sale con `no-routed` aunque vaya sin sello.
- La admision de compute-mode del hook solo cubre `LOCAL_VLLM_ALIASES` y los backends que `resolve_server_id` reconoce por `api_base`; el residente y el alias por nombre directo van por la rama `_served_by_local_vllm`, que no la llama (comentario del hook). Con el residente caido el hook no da 503: el sello corta (residente) o el Router aplica su arista (alias).

## 1. Test que falla primero

`python3 -m pytest tests/test_synapse_resident_fallback_contract.py -p no:cacheprovider -q` sobre el manifest SIN el cambio:

```
FAIL  6 failed, 18 passed
```

Fallan: `test_la_unica_excepcion_de_synapse_es_el_alias_del_clasificador`, `test_synapse_con_cualquier_otro_modelo_sigue_sellada[qwen38-flash-next]` y los cuatro casos de `test_synapse_con_el_residente_directo_no_la_mueve_el_session_router`. En esos cuatro el session router reescribe de verdad (lineas `session_router decision` del log capturado):

```
model=qwen38-flash-next bound=None  plan=alibaba -> default_alibaba (REESCRITA)
model=qwen38-flash-next bound=None  plan=local inflight=99 -> instant_reject_lleno (REESCRITA)
model=qwen38-flash-next bound=alibaba plan=alibaba -> sticky_alibaba (REESCRITA)
model=qwen38-flash-next bound=local plan=local -> rebind_alibaba (REESCRITA)
```

Pasan los 18 que guardan lo que no cambia: el alias sin sello y sin que lo toque el session router (con el residente Ready y caido), `tooling`, `-uncensored` y los demas modelos sellados, `aurora-rca`/`brain`/`claude-cloudblue` selladas con el residente, el cliente que pide no caer, el sello antes del session router, las keys sin sello (`hermes`, `claude-local`) que siguen reescribiendose, y el grafo de aristas del Router.

Con el cambio:

```
PASS  24 passed
```

## 2. Suite completa

`python3 -m pytest tests/ -p no:cacheprovider -q`

```
PASS  920 passed in 116.69s
```

Revision del ConfigMap recalculada con `python3 tests/test_configmap_revision_bump_contract.py --fix` (`config-383f30f586da` -> `config-dd04e1ae7b8b`; rueda el proxy y el `plan-gateway`, que monta el mismo ConfigMap).

## Criterios

- C1 de security, test que falla primero: rojo antes (seccion 1), verde despues.
- `synapse` -> `qwen38-flash-next` con `default_plan=alibaba`, `instant_reject` y el residente Ready: la sirve el residente, sin reescritura y con `disable_fallbacks` (`test_synapse_con_el_residente_directo_no_la_mueve_el_session_router`).
- `synapse` -> `correo-clasificador` con el residente caido: sin sello, sin reescritura del session router, arista `correo-clasificador -> alibaba-q38-flash` en el Router (`test_el_clasificador_solo_llega_a_alibaba_por_la_arista_del_router`).
- Las demas keys no cambian: `aurora-rca`, `brain`, `claude-cloudblue` selladas con el residente; `synapse` con `tooling` sellada; `hermes` y `claude-local` sin sello y con session router.
- No medido aqui: el reparto real tras el despliegue (SpendLogs de la key `synapse`: peticiones a `alibaba-q38-flash` con `attempted_fallbacks=0` deben dejar de aparecer) y el Router real de litellm con el residente caido; este cambio no toca la arista, ya medida en la evidencia de P1 (`dgx-874-correo-clasificador.md`, S1-S3).
