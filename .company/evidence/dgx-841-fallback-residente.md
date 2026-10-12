# DGX-841 · evidencia

Rol: devops · Fecha: 2026-10-12 · Estado: LISTO

## Causa

La key `synapse` esta en `NO_FALLBACK_KEY_ALIASES` (decision del 20-09): el hook le estampa
`disable_fallbacks=True` y el Router (`async_function_with_fallbacks_common_utils`, litellm 1.100.0,
`if disable_fallbacks is True ...: raise e`) devuelve el error original sin mirar `router_settings.fallbacks`.
La arista `qwen38-flash-next -> alibaba-q38-flash` esta bien y SI dispara para el resto de keys (proxy vivo,
10:43 y 11:00 en una replica, 10:47 y 10:52 en la otra: `Falling back to model_group = alibaba-q38-flash`).

## 1. Proxy vivo, antes del cambio (lectura, key `synapse` del pod del adapter, sin imprimirla)

Donde: `kubectl -n synapse exec -i deploy/synapse-adapter-gmail -- python3 - /etc/synapse/credentials/litellm.api_key qwen38-flash-next`
(POST /v1/chat/completions, max_tokens 16). Residente caido: `kubectl -n llm get endpoints qwen38-flash-next` -> `<none>`.

```
FAIL  500 4.6 s {"error":{"message":"litellm.InternalServerError: InternalServerError: OpenAIException - Connection error.",...}}
```

Log del proxy (replica 2, 15:10:32-34): dos intentos del residente (`ConnectionRefusedError ... ('10.43.210.200', 8000)`),
`LiteLLM Retried: 1 times, LiteLLM Max Retries: 1`, sin ninguna linea `Trying to fallback b/w models`.
Los 38 s de it (DGX-831) no son del proxy: el fallo sale en 2-5 s.

## 2. Router real de litellm 1.100.0 con la config del manifest (residente sin nadie escuchando + gemelo Alibaba simulado)

Donde: venv aislado, `litellm==1.100.0`, `router_settings` leido del manifest de esta rama
(`fallbacks`, `num_retries`, `allowed_fails`, `cooldown_time`) y el `timeout`/`cooldown_time` del residente.

```
PASS  sin sello  (disable_fallbacks ausente): OK 2.7s -> 'respuesta de alibaba'
PASS  con sello  (disable_fallbacks=True)   : InternalServerError 2.3s: litellm.InternalServerError: InternalServerError: OpenAIException - Connection error.
```

Sin sello cae a Alibaba en 2.7 s (< 20 s); con sello es exactamente el 500 del proxy vivo.

## 3. Suite del repo

Donde: `python3 -m pytest tests/ -p no:cacheprovider` en esta rama (py3.12.3, pytest 9.0.3).

```
PASS  893 passed in 93.00s
```

Primero el test que falla: `tests/test_synapse_resident_fallback_contract.py` sin el cambio del manifest:
`1 failed, 12 passed` (`test_synapse_con_el_residente_directo_no_se_sella`: `assert True is False`).
Con el cambio: `13 passed`.

## Criterios

- C1: arista del Router intacta y `synapse` + `qwen38-flash-next` ya no se sella (test); el Router cae a Alibaba en 2.7 s (punto 2). Falta la llamada real tras desplegar (ver PR).
- C2: sin cambios en el camino sano: el residente sigue siendo el primer deployment; el fallback solo actua tras el fallo. Se mide en vivo tras desplegar (cabecera `x-litellm-model-api-base`).
- C3: `test_el_router_solo_tiene_la_arista_del_residente` (el mapa es exactamente `{qwen38-flash-next: [alibaba-q38-flash]}`), `test_synapse_con_cualquier_otro_modelo_sigue_sellada` (`tooling`, `qwen38-off`, `-uncensored`, `tooling-uncensored`) y los tests existentes de `-uncensored`.
- C4: de `it`, tras desplegar.
