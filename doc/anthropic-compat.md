# Compatibilidad Anthropic (`/v1/messages`) de LiteLLM con residentes locales

Estado medido el **2026-09-07** contra `https://litellm.lan.e-dani.com` con el residente
`qwen38-flash-next` (imagen desplegada `ghcr.io/berriai/litellm:v1.96.0`). La evidencia
cruda — comando y salida de cada caso — está en el comentario 10914 de
[SC-326](https://e-dani.atlassian.net/browse/SC-326); el objetivo y la decisión de
arquitectura están en la épica [SC-324](https://e-dani.atlassian.net/browse/SC-324).
Los contratos que congelan lo verde viven en `tests/test_anthropic_compat_contract.py`.

## Cómo reproducir la batería

Entorno (solo lectura; la auditoría NO creó ninguna carga en los Sparks):

```bash
export KUBECONFIG=~/.kube/config
kubectl get deploy -n litellm litellm -o jsonpath='{.spec.template.spec.containers[0].image}'
# ghcr.io/berriai/litellm:v1.96.0
kubectl get cm -n comfyui gpu-arbiter-state -o jsonpath='{.data.compute_mode}'
# {"desired_mode":"llm-tp","effective_mode":"llm-tp","phase":"ready","blockers":[]}

# KEY = master key del proxy (no la pegues en compartidos)
KEY=$(kubectl get secret -n litellm litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

# Todas las llamadas llevan estas cabeceras:
#   -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY"
```

Salvo donde se indique, todos los `curl` van a `https://litellm.lan.e-dani.com/v1/messages`
(solo https; http hace 301).

## Semáforo

| Capability | Estado | Motivo / issue upstream |
|---|---|---|
| Tools multi-turno | VERDE | #40132 y #32214 no se reproducen en v1.96.0 |
| `tool_choice: auto` | VERDE | — |
| `tool_choice: any` | **ROJO** | stop_reason `tool_use` sin bloque `tool_use`; sin issue upstream conocido |
| `tool_choice: <tool>` | VERDE con caveat | forzado no estricto (lo decide el residente); bloque `text` vacío acompañante |
| Thinking `enabled` | **ROJO** | no devuelve bloque `thinking` — BerriAI/litellm #29518 |
| Thinking `disabled` / ausente | ROJO menor | devuelve bloque `thinking` igualmente — PR abierto #32337 |
| Streaming sin tools | VERDE | #32357 no se reproduce |
| Streaming con tools | VERDE | `input_json_delta` completo, cero deltas duplicados |
| Streaming con thinking `enabled` | **ROJO** | mismo defecto #29518 |
| Visión | VERDE | — |
| `count_tokens` | VERDE con caveat | #29764 no se reproduce, pero es estimación local que ignora system y tools |
| WebSearch (`web_search_20250305`, PR #66) | VERDE | caveat: las peticiones interceptadas pierden streaming |

## Por capability

### Tools multi-turno — VERDE

El id `chatcmpl-tool-<hex>` hace round-trip intacto: la segunda llamada con el
`tool_result` que referencia ese id continúa la conversación sin error. Los dos issues
sospechosos de la épica — #40132 (tool ids de vLLM rotos) y #32214 (el sanitizador
`sanitize_tool_use_ids_in_anthropic_messages` rompe multi-turno) — **no se reproducen**
en v1.96.0 con este residente.

```bash
# Llamada 1: pedir la tool
curl -sS https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":400,
       "tools":[{"name":"get_weather","description":"Get the current weather for a city",
                 "input_schema":{"type":"object","properties":{"city":{"type":"string","description":"City name"}},"required":["city"]}}],
       "messages":[{"role":"user","content":"What is the weather in Madrid? You MUST call the get_weather tool."}]}'
# → content: [thinking, tool_use id=chatcmpl-tool-...], stop_reason: tool_use

# Llamada 2: reenviar el turno asistente + tool_result con ese id
curl -sS https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":400,
       "tools":[{"name":"get_weather","description":"Get the current weather for a city",
                 "input_schema":{"type":"object","properties":{"city":{"type":"string","description":"City name"}},"required":["city"]}}],
       "messages":[{"role":"user","content":"What is the weather in Madrid? You MUST call the get_weather tool."},
                   {"role":"assistant","content":[{"type":"thinking","thinking":"..."},{"type":"tool_use","id":"chatcmpl-tool-<el id de antes>","name":"get_weather","input":{"city":"Madrid"}}]},
                   {"role":"user","content":[{"type":"tool_result","tool_use_id":"chatcmpl-tool-<el id de antes>","content":"Sunny, 32 degrees Celsius"}]}]}'
# → stop_reason: end_turn, texto con el tiempo de Madrid
```

### `tool_choice: auto` — VERDE

Con prompt que no requiere tool, respuesta de texto normal sin `tool_use`:

```bash
curl -sS https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":300,
       "tools":[{"name":"get_weather","description":"Get the current weather for a city",
                 "input_schema":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}],
       "tool_choice":{"type":"auto"},
       "messages":[{"role":"user","content":"Cual es la capital de Francia? Responde solo con una frase, no necesitas ninguna herramienta."}]}'
```

### `tool_choice: any` — ROJO (sin issue upstream conocido)

`{"type":"any"}` devuelve `stop_reason: tool_use` **sin ningún bloque `tool_use`** en
`content` (tool_use fantasma). Repetido 3 de 3. Un cliente que siga el `stop_reason`
(Claude Code) espera un tool_use que no existe.

Cadena causal, medida y leída en el código de v1.96.0:

1. El adaptador Anthropic→OpenAI traduce `any` a `"required"`
   (`litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py`,
   `translate_anthropic_tool_choice_to_openai`).
2. El residente (SGLang/vLLM) con `tool_choice: "required"` y un prompt que no pide
   ninguna tool responde `finish_reason: "tool_calls"` con `tool_calls: null` — se
   comprueba por la ruta OpenAI directa:

   ```bash
   curl -sS https://litellm.lan.e-dani.com/v1/chat/completions \
     -H "content-type: application/json" -H "Authorization: Bearer $KEY" \
     -d '{"model":"qwen38-flash-next","max_tokens":400,"tool_choice":"required",
          "tools":[{"type":"function","function":{"name":"get_weather","parameters":{"type":"object","properties":{"city":{"type":"string"}}}}}],
          "messages":[{"role":"user","content":"Hola, que tal?"}]}'
   # → finish_reason: tool_calls, tool_calls: null
   ```

3. LiteLLM traduce fielmente `tool_calls` → `tool_use`
   (`_translate_openai_finish_reason_to_anthropic`). El defecto de origen está en el
   residente, amplificado por el adaptador.

**Sin issue upstream conocido** que describa este síntoma exacto (stop_reason `tool_use`
sin bloque `tool_use` con backend OpenAI-compatible). Los más cercanos, no equivalentes:
BerriAI/litellm #19625 (`tool_choice: required` degradado en silencio, proveedor
GigaChat) y #25561 (deltas de tool_use vacíos en streaming con vertex_ai/gemini).

**Workaround:** no usar `tool_choice: {"type":"any"}` con este residente. Usar `auto`
con prompt imperativo (caso verde arriba) o `{"type":"tool","name":...}` con prompt
claro. No hay vía por config: la traducción `any → "required"` es fija en el adaptador
y el que miente con `finish_reason` es el residente.

### `tool_choice: <tool>` — VERDE con caveat

Emite la herramienta pedida (no la otra de la lista) con prompt claro:

```bash
curl -sS https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":400,
       "tools":[{"name":"get_weather","description":"Get the current weather for a city",
                 "input_schema":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}},
                {"name":"get_time","description":"Get the current time for a timezone",
                 "input_schema":{"type":"object","properties":{"tz":{"type":"string"}},"required":["tz"]}}],
       "tool_choice":{"type":"tool","name":"get_weather"},
       "messages":[{"role":"user","content":"Que tiempo hace en Barcelona? Llama a la herramienta."}]}'
```

Caveats medidos: (a) aparece un bloque `text` vacío junto al `tool_use`; (b) el forzado
**no es estricto** — con prompt ambiguo la misma llamada devolvió `end_turn` sin
tool_use, y por la ruta OpenAI directa con función nombrada el residente forzó la tool
EQUIVOCADA (`get_time`). El forzado lo decide el residente, no el proxy.

### Thinking on/off — ROJO invertido (upstream)

- **`thinking: {"type":"enabled", "budget_tokens": N}` → NO llega bloque `thinking`**
  (solo `text`). BerriAI/litellm **#29518** — "/v1/messages adapter drops
  reasoning_content → thinking blocks for OpenAI-compatible chat-completions backends".
  Cerrado upstream por el PR #34433, fusionado a `litellm_internal_staging` el 29-07-2026.
- **`thinking: {"type":"disabled"}` o campo ausente → SÍ llega bloque `thinking`**
  (el residente siempre devuelve `reasoning_content` por la ruta OpenAI y el adaptador
  lo traduce). Justo al revés del contrato Anthropic. PR abierto **#32337**
  ("suppress reasoning_content thinking block when thinking is absent/disabled"); su
  hermano de streaming es **#33241**. Ambos abiertos a 07-09-2026.

```bash
# ROJO: enabled sin bloque thinking
curl -sS https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":400,"thinking":{"type":"enabled","budget_tokens":200},
       "messages":[{"role":"user","content":"Cuanto es 27x43? Responde solo con el numero despues de razonar."}]}'
# → content: [text "1161"], stop_reason: end_turn  (sin bloque thinking)

# "VERDE invertido": disabled SÍ trae bloque thinking
curl -sS https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":400,"thinking":{"type":"disabled"},
       "messages":[{"role":"user","content":"Cuanto es 27x43? Responde solo con el numero."}]}'
# → content: [thinking, text]
```

**Workaround (cliente):** para recibir el razonamiento, **OMITIR el campo `thinking`** —
el default del proxy (hook `low`, SC-203) lo traduce y el bloque `thinking` llega. Para
suprimirlo no hay workaround por petición: el bloque llega igual.

Investigación SC-327 (por qué no hay vía por config): el único comportamiento
específico de `thinking: enabled` en el adaptador de v1.96.0 es
`_route_openai_thinking_to_responses_api_if_needed`
(`adapters/handler.py`), que reenvía el modelo a la Responses API de OpenAI — pensado
para el proveedor OpenAI real, y que además NO llega a disparar con nuestro residente
(el log del router muestra `model=openai/qwen38-flash-next`, sin prefijo `responses/`).
El fallback `reasoning_content → thinking` existe en v1.96.0
(`adapters/transformation.py`, `_translate_openai_content_to_anthropic`) y aun así el
descarte se reproduce: #29518 tiene una segunda causa no cubierta ni por #34433 (cuya
firma `_is_blank_delta` ya estaba en el tarball de v1.96.0). Ningún setting de
`litellm_settings`/`model_info` activa la traducción cuando `thinking` está `enabled`.
Descartado `supports_reasoning: false` como apaño: es mentira como capacidad, lo
contradicen los contratos del repo, y tiene colateral en el health-check de LiteLLM
(`proxy/health_check.py` lo usa para el `max_tokens` de la sonda). Callback propio:
descartado por la regla del CTO — es un bug de adaptación de upstream, no un hueco de
config, y un parche en `httpx`/post-call sería reimplementar la adaptación rota.

### Streaming — VERDE sin tools y con tools; ROJO con thinking enabled

```bash
# Sin tools: secuencia completa, thinking_delta en bloque thinking tipado,
# un solo message_start, cero deltas duplicados (#32357 NO se reproduce)
curl -sSN https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":200,"stream":true,
       "messages":[{"role":"user","content":"Di solo: STREAM-OK"}]}'

# Con tools: el tool_use llega completo por input_json_delta, id valido, JSON parseable
curl -sSN https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":400,"stream":true,
       "tools":[{"name":"get_weather","description":"Get the current weather for a city",
                 "input_schema":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}],
       "messages":[{"role":"user","content":"Que tiempo hace en Tokio? Llama a la herramienta get_weather."}]}'

# ROJO con thinking enabled: mismo defecto #29518, 0 eventos thinking_delta
curl -sSN https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":400,"stream":true,"thinking":{"type":"enabled","budget_tokens":300},
       "messages":[{"role":"user","content":"Di solo: THINK-STREAM-OK"}]}'
```

### Visión — VERDE

El residente ve la imagen (PNG 16x16 rojo real, no 1x1, para descartar descartes por
tamaño — el modelo describe "completamente roja" y responde "Rojo"):

```bash
curl -sS https://litellm.lan.e-dani.com/v1/messages \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","max_tokens":300,
       "messages":[{"role":"user","content":[
         {"type":"image","source":{"type":"base64","media_type":"image/png","data":"iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAaklEQVR4nO3OMREAMACs1L1Q3hjGKgg3mKb5H9uAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBgwYMCAAQMGDBgw8B8wrQcd+H0YbwAAAABJRU5ErkJggg=="}},
         {"type":"text","text":"Que color tiene esta imagen? Responde con una sola palabra."}]}]}'
```

### `count_tokens` — VERDE con caveat

El rojo esperado (#29764, hardcodeo de api.anthropic.com) **no se reproduce**: el
endpoint responde 200 con `input_tokens`. Pero es una **estimación local
modelo-agnóstica**: con un modelo inexistente devuelve el mismo número, y **ignora
`system` y `tools`** (medido: system largo + "Hola" = "Hola" solo = 8).
`POST /v1/token_count` responde 404 (no existe como alternativa).

```bash
curl -sS https://litellm.lan.e-dani.com/v1/messages/count_tokens \
  -H "content-type: application/json" -H "anthropic-version: 2023-06-01" -H "Authorization: Bearer $KEY" \
  -d '{"model":"qwen38-flash-next","messages":[{"role":"user","content":"Hola, como estas? Esta es una frase de prueba para contar tokens."}]}'
# → {"input_tokens":22}  HTTP 200
```

**Workaround para contajes exactos:** no hay endpoint exacto; usar
`usage.input_tokens` de la primera llamada real como referencia, o estimación en cliente
sabiendo que system y tools no se cuentan.

### WebSearch (`web_search_20250305`) — VERDE, con caveat de streaming

Desde el PR #66, Claude Code puede usar WebSearch contra residentes locales: Claude
Code manda la server tool nativa `web_search_20250305`, LiteLLM la traduce a la function
tool `litellm_web_search`, el residente la invoca como tool_use normal y el callback
`websearch_interception` ejecuta la búsqueda (loop agéntico, máx. 3 rondas) contra el
`search_tools` SearXNG local (`http://searxng.chat.svc.cluster.local:8080`, provider
`openai` habilitado explícitamente — el default de upstream es solo `["bedrock"]`).

**Caveat: las peticiones websearch interceptadas pierden el streaming.** La respuesta
final llega completa, no en deltas.

## Decisión de arquitectura (CTO, 07-09-2026, épica SC-324)

**Configuración antes que código, y nunca un LiteLLM propio.** El orden de preferencia
para cerrar cada hueco es: lo que v1.96.0 ya hace por config (`litellm_settings`,
`model_info`, params de provider, `drop_params`) → capability upstream documentada →
callback propio (solo citando docs/código oficial de v1.96.0 que demuestre que no hay
vía por config, y con su issue upstream). NO se forkea LiteLLM, NO se parchea la imagen,
NO se mete proxy ni shim delante.

**Porqué:** la superficie Anthropic es de LiteLLM, no nuestra. Cada línea de callback
propio es código que hay que re-validar en cada bump de imagen y que se queda huérfano
cuando upstream arregla el bug. El coste de mantener un fork o un shim delante del router
supera con creces el de documentar un caveat con su workaround.

**El bump de imagen lo decide el CTO** (no un maker ni el tech lead): LiteLLM es el
router de producción del que cuelgan Codex, OpenClaw, el brain y el panel.

### Resultado de la investigación de config (SC-327, 07-09-2026)

Ninguno de los dos rojos tiene vía por config en v1.96.0 (evidencia de código arriba,
leída del sdist oficial `litellm-1.96.0`):

- `tool_choice: any` → la traducción a `"required"` es fija en el adaptador; no existe
  parámetro que la re-mapee, y el `finish_reason` mentiroso lo emite el residente.
- `thinking: enabled` → no hay flag que active la traducción `reasoning_content →
  thinking` cuando `thinking` está `enabled`; el único camino específico de `enabled` es
  el reenvío a la Responses API de OpenAI, inaplicable a nuestros backends locales.

Quedan como **bug documentado con workaround** (los del semáforo). Sin callback propio:
no hay hueco de config que cerrar, hay bugs de adaptación de upstream (#29518, #32337).

Sobre el bump: la stable más reciente al 07-09-2026 es **v1.100.0** (publicada el
06-09-2026) y trae varios fixes del área Anthropic/thinking (p. ej. PR #37953,
round-trip de bloques thinking a backends OpenAI — que arregla #24985, **no** #29518).
Ninguna release note posterior a v1.96.0 declara arreglado #29518 ni el `tool_choice:
any` fantasma, y la firma del fix #34433 ya estaba en v1.96.0. La recomendación viaja
al CTO en el PR; aquí no se sube nada.
