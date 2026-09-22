# Plan: clase de sesión `x-claude-class` — la compañía nunca salta a Alibaba (INFRA-208)

Fecha: 2026-09-22 · Repo: k8s-litellm-pocharlies (tronco `main`, lo confirma el ArgoCD
app `litellm`: `targetRevision: main`, path `k8s`) · Hook: `dev/session_router.py`
(publicado en el ConfigMap `litellm-config`).

## Contexto

El hook tiene la precedencia sellado > admisión > plan explícito > sticky >
default+instant_reject. Con `instant_reject=true`, una sesión de plan default cuyo
residente está saturado (en vuelo ≥ `local_slots`) se reescribe a
`alibaba-q38-flash` y, con sticky activo, se re-binde su sticky a `alibaba` durante
1 h. Decisión de Dani (21-09-2026): **las sesiones de la COMPANÍA (supervisor/
headless) nunca deben saltar a Alibaba — tienen que encolar** (comportamiento
20-09: la admisión compute-mode de `litellm_strip_params.py` encola, timeout 240 s);
las demás sesiones sí pueden saltar. Hot-fix ya aplicado mientras tanto:
`instant_reject=false` global (medido en el panel el 22-09:
`{"sticky":true,"instant_reject":false,"local_slots":8,"default_plan":"local"}`).
Este PR es la clasificación definitiva para poder reactivar `instant_reject` sin
quemar a la compañía.

## Diseño

El wrapper de la compañía (x86-host-runtime, **otro PR**, no toca este repo) mandará
la cabecera `x-claude-class: company`. El hook:

1. **Lee la cabecera** de las fuentes que YA tiene en `data` (ver "vía real" abajo).
   Búsqueda **case-insensitive** por clave y valor normalizado.
2. **`class=company` ⇒ la válvula `instant_reject` no aplica**: la petición sigue la
   ruta normal (sticky local, y si el residente está lleno, la admisión de
   strip_params ENCOLA). Cero reescrituras a alibaba, cero re-bind sticky a alibaba
   por la válvula.
3. **Sin cabecera ⇒ comportamiento idéntico al actual** (las demás sesiones saltan).
4. **Fail-open**: cualquier error leyendo la cabecera = sin clase = comportamiento
   actual. El helper no lanza nunca; además `apply_session_routing` ya envuelve todo
   en try/except.

### Vía real de la cabecera (verificada, no supuesta)

Desplegado: `ghcr.io/berriai/litellm:v1.100.0@sha256:c8756e7b…` (digest pineado en el
manifest). Fuente exacta de esa versión (`litellm/proxy/litellm_pre_call_utils.py`):

- `add_litellm_data_to_request` (l. 1685) construye
  `_logging_safe_headers = redact_credential_headers(clean_headers(request.headers))`
  (l. 1770) y lo asigna **incondicionalmente** a
  `data["metadata"]["headers"]` (l. 1822 y l. 2083; `_metadata_variable_name` es
  `"metadata"` salvo rutas thread/assistant, l. 561-579).
- `clean_headers` (l. 885) solo excluye cabeceras especiales de auth/litellm; una
  cabecera custom como `x-claude-class` **sobrevive**. `redact_credential_headers`
  solo enmascara valores de credenciales (authorization, x-api-key…), no claves.
- **Trampa de casing**: `clean_headers` guarda las claves con la **caja tal cual
  llegó por el cable** (starlette preserva la caja original al iterar; compara en
  minúsculas pero no normaliza el dict resultante). El SDK de Anthropic/node http
  pueden mandar `x-claude-class` o `X-Claude-Class` según transporte (HTTP/2 baja a
  minúsculas, HTTP/1.1 conserva). Por eso la lectura es **case-insensitive** — un
  `headers.get("x-claude-class")` a pelo sería el bug.
- `data["headers"]` (raíz) SOLO existe con `forward_client_headers_to_llm_api`
  activado (l. 1166-1173, 1203-1210) y además re-mapeada con prefijo `x-litellm-`;
  en este despliegue está **apagado** (no está en `general_settings` del
  `config.yaml`). La fuente buena es `data["metadata"]["headers"]`; el helper lee
  ambas por robustez (la raíz tal como la lee ya `_session_id`).

**Prueba en vivo contra el router del clúster (22-09-2026)**: `POST /v1/messages` con
`x-litellm-session-id: probe-class-1790035188-3382190` y `x-claude-class: company`
(modelo `tooling`, max_tokens=1, key de sesión del wrapper local — no la master) →
HTTP 200 servido por el residente; en Valkey apareció
`session-router:sticky:probe-class-1790035188-3382190 = "local"` (TTL 3555 s) — el
hook ejecutó para esa petición con la identidad derivada de cabeceras y decidió
`local` sin reescribir (instant_reject está apagado hoy). Clave de sonda borrada
después. Esto prueba el camino petición→hook con cabeceras custom; la presencia de
`x-claude-class` en `data["metadata"]["headers"]` la prueba la fuente de arriba.

### Qué NO cambia (C3 — precedencia intacta)

El gate cubre **solo el bloque de la válvula** (`config["instant_reject"] and
model == RESIDENT_MODEL`). Para sesiones `company` siguen aplicando, exactamente
igual que hoy:

- sellado (`disable_fallbacks`) y uncensored → no-op (van antes);
- plan EXPLÍCITO del panel (`session_plans[sid]`), incluido `alibaba` → reescribe
  (orden del operador, no la válvula);
- sticky ya ligado a `alibaba` (p.ej. por `default_plan=alibaba` o por un re-bind
  previo a este cambio, TTL 1 h) → sigue reescribiendo hasta que expire;
- `resident_ready=false` (compute-mode no admite el residente) → `rebind_alibaba`
  sigue aplicando: no es saturación, es que el residente no está; encolar sería un
  503 seguro. Es la ruta de disponibilidad, no la válvula.

`company` **no salta cola**: la supresión de la válvula devuelve el control a la
admisión compute-mode de strip_params, que es la que encola (semáforo 8 + timeout
240). No se añade ninguna ruta que evada la admisión.

**Cabecera falsificable**: quien llegue a LiteLLM con una key puede mandar
`x-claude-class: company`. El único efecto es ENCOLAR en vez de saltar a Alibaba
bajo saturación — auto-perjuicio, sin escalada de privilegios (no toca sellado,
admisión ni planes). Se documenta aquí y en CONTRACTS.yaml; no merece gate.

### Superficie de contrato (C5)

El repo no tenía `CONTRACTS.yaml` (el mecanismo por repo es ese, según la skill
`synapse-contracts`; el checker `check-contracts.py` del pre-push global no existe
en la ruta local, el hook lo salta si falta). Se crea `CONTRACTS.yaml` en la raíz
con el formato real de los repos de la casa (`version: 1`, `registry:`, `contracts:
[id, kind, value, role, files, status, note]`), entrada nueva:

- `id: dgx.claude.class-header.v1`, `kind: http-header`, `value: x-claude-class`,
  `role: consumer` (publisher: el wrapper de x86-host-runtime, otro PR; consumer:
  este hook).

El commit del código lleva el trailer `Contract-Change: add
dgx.claude.class-header.v1` y marcador `# CONTRACT: dgx.claude.class-header.v1`
sobre la lectura en el código.

## Criterios de aceptación

- **C1**: sesión company + residente saturado + `instant_reject=true` ⇒ NO reescribe
  a alibaba, NO re-binde sticky a alibaba; la decisión cae a `local` y la admisión
  de strip_params encola (el hook devuelve False sin tocar `data`).
- **C2**: sin cabecera ⇒ comportamiento idéntico al actual con `instant_reject=true`
  (salta a alibaba y, con sticky, re-binde).
- **C3**: la cabecera no esquiva sellado, uncensored, plan explícito ni sticky
  ligado a alibaba (precedencia intacta; company solo desactiva la válvula).
- **C4**: fail-open — `headers` no-dict, valores raros, metadata extraña ⇒ sin
  excepción y sin cambio de comportamiento.
- **C5**: `CONTRACTS.yaml` con la entrada `dgx.claude.class-header.v1` (arriba) y
  trailer `Contract-Change` en el commit.

## Tests

En `tests/test_session_routing_contract.py`, con el patrón `_Env` existente (stub de
las cuatro E/S + `apply_session_routing` sobre el módulo ejecutado del ConfigMap):
C1 (con sticky on y off), C2 (incluye el re-bind sticky que hoy hace
`test_valvula_sin_sticky_reescribe_al_instante` pero con sticky on), casing
alternativo de clave y valor, C3 (sellado, uncensored, plan explícito alibaba,
sticky bound alibaba, rebind por residente no-ready), C4 (headers corruptos), y
observabilidad (`class=` en la línea de decisión). Suite entera verde en local
igual que el job `litellm-contracts` de CI.

## Despliegue (este PR NO aplica nada)

Lo desplegable es el ConfigMap `litellm-config` del manifiesto: el hook se actualiza
en `dev/session_router.py` **y** en la copia embebida del ConfigMap en el MISMO
commit (hoy están byte a byte idénticos; no hay test que lo fuerce — trabajo
adyacente sugerido, fuera de scope). Como el proxy NO relee el ConfigMap en
caliente, el commit bumpea la anotación `config.k8s.e-dani.com/revision` del
Deployment `litellm` con `python3 tests/test_configmap_revision_bump_contract.py
--fix` (el contrato lo exige). ArgoCD lo aplica al mergear; el rollout reinicia el
proxy con el código nuevo.

## Fuera de scope

- El publisher (poner la cabecera en el wrapper): PR en x86-host-runtime.
- Reactivar `instant_reject` desde el panel: operación posterior, decisión de Dani.
- Test de deriva `dev/` ↔ ConfigMap embebido.
