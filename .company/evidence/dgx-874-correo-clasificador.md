# DGX-874 · P1 · evidencia

Rol: devops · Fecha: 2026-10-13 · Estado: LISTO

Donde: x86 (`ubuntu`), worktree de `dgx-874-correo-clasificador` cortado de `origin/main` 8d8ecdd; python 3.12.3, pytest 9.0.3.

## 1. Test que falla primero

`python3 -m pytest tests/test_correo_clasificador_alias_contract.py -p no:cacheprovider -q` sobre el manifest SIN el cambio:

```
FAIL  8 failed, 5 passed
```

Fallan: alias ausente (`hay 0` deployments), `timeout`/`num_retries`/`cache_salt`, `cooldown_time`/efforts, la arista del Router (`KeyError: 'correo-clasificador'`), `synapse` + alias sellada (`assert True is False`) y la peticion real del adapter. Pasan los 5 que guardan lo que no cambia (`synapse` + `tooling` sellada, `aurora-rca`/`brain`/`claude-cloudblue` selladas con el alias, el alias fuera de `THINKING_TIERS` y `SWAPPABLE_ALIASES`). Con el cambio:

```
PASS  13 passed
```

## 2. Tests existentes

Los cuatro del spec, sin tocarlos: `test_family_sampling_contract`, `test_chat_catalog_contract`, `test_aurora_fallback_policy_contract`, `test_configmap_revision_bump_contract` (63 passed junto al nuevo). Revision del ConfigMap recalculada con `--fix` (`config-383f30f586da`; rueda el proxy y el `plan-gateway`, que monta el mismo ConfigMap).

Cuatro tests mas fijan la lista exacta de algo que el alias nuevo amplia, rojos tras el cambio y actualizados a proposito (cada uno dice por que en su comentario):

- `test_litellm_network_contract::test_every_litellm_request_timeout_is_600_seconds`: excepcion `correo-clasificador: 6` declarada con su motivo.
- `test_local_backends_vision_contract::test_estan_todos_los_alias_locales_esperados`: el alias entra en `ALIAS_LOCALES` (hereda `supports_vision: true` del residente por ancla).
- `test_synapse_resident_fallback_contract` (C3 de DGX-841) y `test_tooling_profile_contract::test_proxy_fallbacks_never_leave_local_models` (lista blanca del 20-09): el grafo de fallbacks pasa de una arista a las dos, `qwen38-flash-next` y `correo-clasificador`, ambas a `alibaba-q38-flash`; `tooling` y `-uncensored` siguen sin salto.

Suite completa: `python3 -m pytest tests/ -p no:cacheprovider -q`

```
PASS  910 passed in 115.15s
```

## 3. Router real de litellm 1.100.0 (la version pineada) con la config del manifest

Donde: venv aislado con `litellm==1.100.0`, script de usar y tirar fuera del repo. Lee del manifest de esta rama el `model_list` (`correo-clasificador`, `alibaba-q38-flash`) y `router_settings` (`fallbacks`, `num_retries` 1, `allowed_fails` 3, `cooldown_time` 300, `timeout` 600); solo cambia los `api_base` por stubs locales (residente colgado / en 500 / sin nadie escuchando; Alibaba que contesta).

```
manifest: correo-clasificador litellm_params.timeout=6 num_retries=0 model_info.cooldown_time=60
manifest: router_settings num_retries=1 allowed_fails=3 cooldown_time=300
S1 correo-clasificador, residente colgado: 6.5 s -> SERVED-BY-ALIBABA-STUB
S2 variante sin num_retries 0, residente colgado: 14.2 s -> SERVED-BY-ALIBABA-STUB
S3 correo-clasificador, residente sin endpoints: 0.0 s -> SERVED-BY-ALIBABA-STUB
S4 12 peticiones con el residente en 500: golpes al residente 12, cooldowns activos []
S5 variante con allowed_fails_policy (InternalServerErrorAllowedFails 2): tras 3 peticiones, golpes 3, cooldown [60] s; peticion 4: 0.01 s -> SERVED-BY-ALIBABA-STUB, golpes al residente 3
PASS S1 PASS S2 PASS S3 PASS S4 PASS S5
```

- S1/S2: con `timeout: 6` y `num_retries: 0` el respaldo llega a los 6,5 s; sin el cero, a los 14 s (dos intentos y la espera entre ellos). Es lo que justifica el `num_retries: 0`.
- S4 (hallazgo, contradice el comentario de `router_settings` del 20-09, que leia litellm 1.96): con litellm 1.100.0 el `allowed_fails: 3` del router es el defecto de la libreria y se lee como «no puesto»; un grupo de UN deployment no entra en cooldown por timeouts ni 5xx. Consecuencia: `cooldown_time: 60` no llega a actuar y cada peticion prueba antes al residente (el local primero, y cuesta como mucho 6 s). Vale igual para `qwen38-flash-next`.
- S5 (hipotesis, no esta en el manifest): `model_info.allowed_fails_policy` es el mando por deployment que el Router si lee para aparcar; con el, el valor aparcado es el `model_info.cooldown_time`. No se pone: aparcado, todo el correo del alias iria a Alibaba mientras dure.

## Criterios

- P1: test del alias en verde (rojo antes, seccion 1) y tests existentes del manifest en verde (seccion 2).
- No medido aqui, es de L1: allowlist de la key `synapse`, llamada real, respaldo con `mock_testing_fallbacks`, y si Alibaba contesta sin razonamiento con el cuerpo que deja el hook (`chat_template_kwargs.enable_thinking: false`, que Alibaba no lee a ese nivel).
