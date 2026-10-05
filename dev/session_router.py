# INFRA-208 (21-09-2026): sticky routing por sesión + rechazo instantáneo REAL.
#
# NO ES UN SEGUNDO CALLBACK (veredicto del arquitecto, mandato 2): este módulo
# lo IMPORTA litellm_strip_params.py y lo llama desde su async_pre_call_hook,
# justo después de la RED FINAL de visión y antes del sampling por familia. Ahí
# data["model"] ya está resuelto (tooling/qwen38-off -> qwen38-flash-next), los
# gates uncensored/openrouter ya pasaron, y el rewrite —si lo hay— llega ANTES
# del sello, del template strict-system, de la admisión compute-mode y del
# tracker de peticiones activas, que ven todos el modelo final.
#
# QUÉ POSEE Y QUÉ NO (mandato 1):
#   - Valkey dedicado (litellm-valkey): el mapa sticky sid -> "local" |
#     "alibaba", el HASH de sesiones `session-router:sessions` (sid ->
#     json{t,ts}: prompt estimado y última actividad de las peticiones
#     servidas por el residente — válvula de presupuesto KV, 27-09-2026) y
#     las claves de nacimiento `session-router:albind:<sid>` (SETNX, TTL 24 h:
#     desde cuándo la sesión está ligada a Alibaba — regla de retorno).
#     Prohibido INCR/DECR/ZSET: no hay contador aquí. RELAJACIÓN DEL MANDATO
#     (27-09-2026, lo pidió Dani): el HASH NO es un contador — cada entrada
#     la ESCRIBE el propio pod que enruta (HSET, idempotente por sid, con su
#     marca de tiempo) y el total se saca LEYENDO (HGETALL en un sondeo en
#     background + poda por HDEL de las entradas muertas). No existe ningún
#     acumulador repartido entre pods: si Valkey se vacía, el presupuesto se
#     reconstruye solo con las siguientes peticiones. Justificación: el tope
#     local_slots cuenta peticiones EN VUELO; las sesiones ociosas con un
#     prefijo de 150k en la KV cache no cuentan y vLLM las expulsa del LRU
#     igualmente — al volver re-prefrían frías (decenas de segundos, decode
#     1-3 tok/s) y montan un bucle de thrash. Quién ocupa la cache lo sabe
#     solo quien enruta, así que se anota al enrutar.
#   - Peticiones en vuelo al residente: la fuente es el ActiveRequestTracker
#     que YA existe (in-process en este pod, exacto, sin el throttle de 0,25 s
#     del fichero) + el sidecar :4001 para las OTRAS réplicas, deduplicando por
#     request_id para no contar dos veces las filas locales que el sidecar
#     agrega desde su fichero.
#   - Cooldown (mandato 5): nunca se enruta ni se pega una sesión a un backend
#     en cooldown. Se consulta la API pública del Router en proceso
#     (get_model_ids + cooldown_cache.get_active_cooldowns), nunca claves de
#     cache a mano. Si la consulta falla -> degradar: sin stick y sin rewrite
#     (la petición encola = comportamiento actual).
#
# PRECEDENCIA EXPLÍCITA (mandato 6):
#   1. sellado (disable_fallbacks) y uncensored -> no-op (mandato 4).
#   2. admisión compute-mode -> vive en strip_params (marcador, mandato 3).
#   3. plan EXPLÍCITO del panel (session_plans[sid]): local -> residente y
#      ENCOLA si está lleno, NUNCA rechazo instantáneo (es decisión del
#      usuario); alibaba -> alibaba; claude -> no-op aquí (las peticiones
#      Anthropic ni pasan por LiteLLM; la puerta vive en el claude-router x86).
#   4. sticky (solo sesiones de plan default): binding en Valkey; destino solo
#      si está sano (residente: compute-mode listo y, con la válvula activa,
#      hueco < local_slots; alibaba: sin cooldown); si hay hueco se re-vincula.
#   4b. VÁLVULA DE PRESUPUESTO KV (27-09-2026): una sesión NUEVA (sin binding)
#      que pediría el residente se liga a Alibaba si su prompt estimado + la
#      suma de los prompts de las sesiones locales VIVAS (actividad en los
#      últimos session_idle_s) supera kv_budget_pct de la capacidad KV total
#      del motor. La capacidad se descubre en runtime (vllm:cache_config_info:
#      la etiqueta kv_cache_size_tokens — en el híbrido el producto
#      num_gpu_blocks × block_size sobreestima ~14 %, el estado Mamba come
#      cache —, en el MISMO sondeo de /metrics que ya lee
#      la cola; último-bueno, nunca un número fijo — al cambiar el residente
#      cambia la cache). Sin capacidad conocida o sin lectura fresca del HASH
#      la válvula está APAGADA (fail-open = comportamiento actual). Una sesión
#      YA ligada a local NUNCA la expulsa esta válvula: desalojar un prefijo
#      caliente paga el re-prefrío de quien se va y el de quien vuelve. Las
#      sesiones BOT (clase company o key alias en bot_keys: hermes,
#      aurora-rca) solo se admiten bajo el umbral más bajo bot_budget_pct:
#      el interactivo (claude-cli, opencode, open-webui) va primero.
#   4c. RETORNO desde Alibaba (27-09-2026): una sesión ligada a Alibaba vuelve
#      al residente SOLO si (a) su prompt actual es pequeño (<=
#      return_max_tokens, o sea compactó) y cabe en el presupuesto, o (c)
#      nació en Alibaba hace >= alibaba_return_after_s y cabe. El TTL sticky
#      de Alibaba subió de 300 s a 3600 s para que una pausa corta no devuelva
#      fría a una sesión de 150k a mitad de conversación. Nunca vuelve con el
#      residente no Ready, con default_plan=alibaba o con un plan EXPLÍCITO
#      del operador (ni pasa por aquí: sale antes por precedencia 3).
#   5. default con instant_reject: al llegar a local_slots en vuelo, reescribe
#      al instante a alibaba-q38-flash. SOLO sesiones de plan default.
#   El sticky en Valkey se escribe ÚNICAMENTE para sesiones de plan default.
#   El HASH de sesiones y las claves albind se escriben TAMBIÉN solo para
#   sesiones de plan default, salvo la anotación de consumo de una plan
#   EXPLÍCITO local (ocupa cache igual que cualquier sesión y debe contar en
#   la suma; su binding no se toca).
#
# REESCRIBIR, NO EXCEPCIÓN (respuesta D del arquitecto): las excepciones de un
# pre_call hook llegan al cliente tal cual; el fallback vive aguas abajo en
# async_function_with_fallbacks. Precedentes: capability chains y desvío de
# visión. Todo rewrite residente -> alibaba marca metadata
# `_session_routing_rerouted` y la admisión compute-mode de strip_params lo
# respeta (`not session_rerouted`), igual que `not vision_diverted`: si no, el
# instant_reject hacia alibaba recibiría un 503 de admisión vía proxy_model
# justo cuando el cómputo local está apagado, que es cuando más falta hace.
#
# FAIL-OPEN TOTAL: cualquier fallo (panel, Valkey, sidecar, cooldown) devuelve
# False sin tocar data. Presupuesto por petición: ≤100 ms por operación
# (config TTL 5 s stale-while-revalidate: el camino de la petición NO hace I/O,
# refresca en background con último-bueno; Valkey y sidecar con timeout duro de
# 0,1 s). Valkey caído => LiteLLM sigue EXACTAMENTE como hoy.
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

log = logging.getLogger("session_router")

RESIDENT_MODEL = os.environ.get("SESSION_ROUTER_RESIDENT_MODEL", "qwen38-flash-next")
OVERFLOW_MODEL = os.environ.get("SESSION_ROUTER_OVERFLOW_MODEL", "alibaba-q38-flash")
# Los únicos dos nombres sobre los que este módulo decide. Todo lo demás
# (tooling sin resolver, or-*, claude-*, *-uncensored, 27b...) pasa de largo.
ROUTED_MODELS = frozenset({RESIDENT_MODEL, OVERFLOW_MODEL})

REDIS_URL = os.environ.get(
    "SESSION_ROUTER_REDIS_URL",
    "redis://litellm-valkey.litellm.svc.cluster.local:6379/0",
)
# CONTRACT: dgx.model-routing.config.v1
CONFIG_URL = os.environ.get(
    "MODEL_ROUTING_CONFIG_URL",
    "http://dgx-dashboard-backend.control-nexus.svc.cluster.local:9002/api/model-routing/config",
)
SIDECAR_URL = os.environ.get(
    "SESSION_ROUTER_SIDECAR_URL",
    "http://127.0.0.1:4001/internal/active-requests",
)

CONFIG_TTL_SECONDS = 5.0
# ≤100 ms por operación en el camino de la petición (mandato 10). La config ya
# NO está en el camino de la petición (stale-while-revalidate, ver _config);
# el tope sigue aquí como contrato y para cualquier uso síncrono futuro.
CONFIG_TIMEOUT_SECONDS = 0.1
# Timeout del refrescador EN BACKGROUND: fuera del camino de la petición, nunca
# bloquea un request, así que no compite con el presupuesto de 100 ms. Medido en
# el pod (21-09): /api/model-routing/config tarda 130-300 ms SIEMPRE porque el
# backend paga un subprocess kubectl por lectura — un fetch síncrono con tope de
# 100 ms timeoutearía en cada intento y el panel no propagaría nunca (C8).
CONFIG_REFRESH_TIMEOUT_SECONDS = 2.0
REDIS_OP_TIMEOUT_SECONDS = 0.1
# Timeout de la ESCRITURA sticky, que va en background (fire-and-forget, fix del
# defecto 1 del QA live 21-09: con 100 ms en el camino de la petición ~99% de los
# sticky_set morían bajo carga y el binding no aterrizaba nunca). Fuera del
# camino de la petición => no compite con el presupuesto del mandato 10.
STICKY_WRITE_TIMEOUT_SECONDS = 2.0
# PING de keepalive del pool de redis: mantiene la conexión caliente y evita
# pagar DNS+TCP+AUTH en la siguiente operación (que sí va en camino de petición).
REDIS_HEALTH_CHECK_INTERVAL = 30
# 25-09-2026: el recuento de las OTRAS réplicas sale del camino de la petición.
# Medido en producción: el agregado del sidecar tarda ~160 ms (resuelve por DNS el
# Service headless de pares y CoreDNS vive en ks5-cp-2/3, que desde el x86 van por
# el relay DERP de Tailscale). Con el tope síncrono de 100 ms fallaba SIEMPRE, cada
# pod solo se contaba a sí mismo y el tope real era local_slots × réplicas. Ahora lo
# refresca un sondeo en background (mismo patrón que el de la cola de vLLM) y la
# válvula lee la última lectura buena: 0 ms en el camino de la petición.
SIDECAR_LOCAL_URL = os.environ.get(
    "SESSION_ROUTER_SIDECAR_LOCAL_URL",
    "http://127.0.0.1:4001/internal/active-requests/local",
)
SIDECAR_POLL_SECONDS = 0.5
SIDECAR_POLL_TIMEOUT_SECONDS = 2.0
# Sin lectura buena en este tiempo el dato de pares no vale: solo cuenta este pod.
SIDECAR_STALE_SECONDS = 3.0
# 23-09-2026 (Dani): TTL DE INACTIVIDAD. El vínculo se RENUEVA en cada petición
# de la sesión: mientras la sesión trabaja se queda donde está su caché; cuando
# caduca, la siguiente petición vuelve a decidirse.
# 26-09-2026 (Dani): distinto por destino (antes 10 min para los dos).
#   local   30 min: la caché de prefijo del vLLM es gratis; una sesión local
#           sigue en local tras una pausa.
#   alibaba 60 min (27-09-2026; antes 5 min): con la válvula de presupuesto
#           KV (precedencia 4b) una sesión de 150k que pausaba 5 min volvía al
#           local FRÍA a mitad de conversación y pagaba el re-prefill expulsando
#           a otra. El retorno desde Alibaba es ahora una REGLA (compactó y
#           cabe, o nació hace >= alibaba_return_after_s y cabe), no el
#           vencimiento del TTL. La caché implícita de Alibaba no aguanta
#           pausas largas de todas formas; sin pausa se renueva y no se mueve.
STICKY_TTL_LOCAL_SECONDS = int(os.environ.get("SESSION_ROUTER_STICKY_TTL_LOCAL_S", "1800"))
STICKY_TTL_ALIBABA_SECONDS = int(os.environ.get("SESSION_ROUTER_STICKY_TTL_ALIBABA_S", "3600"))
# CONTRACT: dgx.session-router.sticky-key.v1
STICKY_KEY_PREFIX = "session-router:sticky:"
DEFAULT_LOCAL_SLOTS = 8
LOCAL_SLOTS_CAP = 64
PLANES = ("local", "alibaba", "claude")

# ── Válvula de presupuesto KV por sesión (27-09-2026, precedencia 4b/4c) ────
# local_slots cuenta peticiones EN VUELO; una sesión ociosa con su prefijo de
# 150k en la KV cache no cuenta y vLLM la expulsa del LRU cuando entran más de
# las que caben (27-09: 2.775.211 tokens en el residente TP=2). Al volver
# re-prefría fría. Esta válvula presupuesta la cache por SESIÓN: cada
# petición servida por el residente anota su prompt estimado en el HASH
# `session-router:sessions` (sid -> json{t,ts}); un sondeo en background lo
# suma (solo las activas en session_idle_s) y una sesión NUEVA que no quepa en
# kv_budget_pct de la capacidad nace en Alibaba. La capacidad llega de
# vllm:cache_config_info (la etiqueta exacta kv_cache_size_tokens; el producto
# num_gpu_blocks × block_size solo como fallback de vLLM viejos), leída en el MISMO
# sondeo de /metrics que ya mira la cola: el presupuesto es un PORCENTAJE,
# nunca un número fijo de tokens, porque al cambiar el residente cambia la
# cache. Sin capacidad conocida o sin lectura fresca del HASH => válvula
# APAGADA (fail-open = comportamiento actual). Los sondeos viven fuera del
# camino de la petición (0 ms); la decisión lee solo cachés en memoria.
# CONTRACT: dgx.session-router.sessions-hash.v1
SESSIONS_HASH_KEY = "session-router:sessions"
# Nacimiento de la sesión en Alibaba (regla de retorno 4c): SETNX — el primer
# vínculo manda y se renueva el TTL; se BORRA al volver a local. Medido desde
# el nacimiento, no desde la última actividad.
ALBIND_KEY_PREFIX = "session-router:albind:"
ALBIND_TTL_SECONDS = 86400
KV_POLL_SECONDS = float(os.environ.get("SESSION_ROUTER_KV_POLL_S", "1"))
KV_POLL_TIMEOUT_SECONDS = 2.0
# Sin lectura buena del HASH en este tiempo la válvula no actúa (como la cola).
KV_STALE_SECONDS = 5.0
DEFAULT_KV_BUDGET_PCT = 85
DEFAULT_BOT_BUDGET_PCT = 60
DEFAULT_SESSION_IDLE_S = 600
DEFAULT_RETURN_MAX_TOKENS = 30000
DEFAULT_ALIBABA_RETURN_AFTER_S = 3600
# Bots por alias de virtual key (medido en SpendLogs; los interactivos —
# claude-local SIN clase company, opencode-*, open-webui — NO están). La
# identificación es fiable para los listados: cada bot usa su key. Una key
# nueva sin listar se trata como interactiva (fail-open hacia local).
DEFAULT_BOT_KEYS = ("hermes", "aurora-rca")
# Punto de partida del estimador; el tracker aprende el real por modelo
# (active_request_tracking.py: chars_per_token arranca en 3,5).
FALLBACK_CHARS_PER_TOKEN = 3.5
# Estado en memoria: lo escriben los sondeos en background, el camino de la
# petición SOLO lee. tokens=None => capacidad desconocida => válvula apagada.
_kv_capacity = {"tokens": None, "read_at": None}
_kv_sessions = {"total": None, "n": None, "read_at": None}
_kv_poller = None
_kv_no_capacity_logged = False


def _cfg_int(config, key, default):
    """Entero de la config saneada, con default si falta o tiene otra forma
    (los cfg de los tests no traen los campos nuevos: misma segunda red que
    _sanitize, pero en el lector)."""
    value = config.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default

# Mandato 4b: los alias abliterados NUNCA caen a Alibaba (sello de #101, que
# se estampa en strip_params DESPUÉS de este punto de inserción — por eso la
# comprobación por NOMBRE aquí es obligatoria, el sello aún no existe).
UNCENSORED_ALIASES = frozenset({
    "tooling-uncensored", "qwen38-u-off",
})
UNCENSORED_SUFFIX = "-uncensored"

# INFRA-208 (22-09): clase de sesión. El wrapper de la compañía (x86-
# host-runtime) estampa x-claude-class: company; con ese valor la válvula
# instant_reject NO aplica — la sesión sigue a la admisión de strip_params,
# que ENCOLA (decisión de Dani 21-09: la compañía nunca salta a Alibaba).
# Sin cabecera u otro valor, comportamiento actual. Superficie de contrato:
# CONTRACTS.yaml dgx.claude.class-header.v1 (publisher: el wrapper x86;
# consumer: este hook).
CLASS_HEADER = "x-claude-class"
COMPANY_CLASS = "company"

# Interruptor «Fallback Alibaba» de la compañía (23-09-2026). El panel Settings de
# /claude-sessions lo guarda en control-nexus/company-control y el dashboard lo sirve
# en el campo ADITIVO `company` de /api/model-routing/config (contrato
# dgx.model-routing.config.v1). Con `company.alibaba = false`, una petición de clase
# company (a) se SELLA con disable_fallbacks — el Router no cae a alibaba-q38-flash y
# este mismo módulo la ve "sellada" y no la reescribe por sticky, plan ni re-bind — y
# (b) si pide un `alibaba-*` explícito se rechaza con 403. Solo la compañía: el resto de
# consumidores conserva su fallback. `company.claude` no se usa aquí (Anthropic ni pasa
# por LiteLLM): lo aplica el claude-router del x86.
ALIBABA_PREFIX = "alibaba-"
DEFAULT_COMPANY = {"claude": True, "alibaba": True}

# Interruptores «Fallbacks a Alibaba» (25-09-2026, Dani: poder dejar de usar Alibaba sin
# PR). Los guarda el panel /inferencia (tarjeta ALIBABA · TOKEN PLAN) en el campo ADITIVO
# `alibaba` de /api/model-routing/config. Uno por cada camino por el que el tráfico acaba
# en Alibaba sin pedirlo por nombre:
#   router_fallback  — el fallback del Router qwen38-flash-next -> alibaba-q38-flash
#                      (strip_params estampa `fallbacks: []` por petición)
#   tooling_fallback — TOOLING_FALLBACKS de strip_params (tooling sin residente Ready)
#   demos_fallback   — KEY_TOOLING_FALLBACKS de strip_params (key `demos`)
#   overflow         — TODA reescritura residente -> Alibaba de ESTE módulo (sticky,
#                      válvulas de capacidad, planes alibaba por defecto o por sesión)
# Solo un false bool apaga; ausente, null o basura = encendido (= antes del 25-09).
DEFAULT_ALIBABA = {
    "router_fallback": True, "tooling_fallback": True,
    "demos_fallback": True, "overflow": True,
}

# ── Sin esperas en el residente (23-09-2026, Dani: "no quiero que nunca se espere") ──
# Medido ese día: el semáforo de LiteLLM casi no espera (p99 0,25 s); la espera es la
# COLA DE vLLM (p50 8 s, p95 94 s en 6 h), y con 3-5 peticiones en marcha también: el
# prefill de un contexto largo frío va de uno en uno (4096 tokens/paso, T=0) y todo lo
# nuevo espera detrás. Por eso la válvula no mira solo cuántas hay en vuelo, sino si la
# cola de vLLM lleva WAIT_TOLERANCE_SECONDS sin vaciarse: entonces lo nuevo sale a
# Alibaba. La lee un sondeo en BACKGROUND de /metrics del residente (el camino de la
# petición no hace I/O); dato viejo o sondeo caído => sin desvío (fail-open).
#
# Un solo presupuesto para el residente: todos los nombres que acaban en el mismo vLLM
# (el directo y su gemelo abliterado; tooling/qwen38-off se reescriben antes a estos)
# cuentan juntos contra local_slots.
RESIDENT_FAMILY = frozenset(
    n.strip() for n in (
        os.environ.get("SESSION_ROUTER_RESIDENT_FAMILY")
        or f"{RESIDENT_MODEL},{RESIDENT_MODEL}-uncensored"
    ).split(",") if n.strip()
)
VLLM_METRICS_URL = os.environ.get(
    "SESSION_ROUTER_VLLM_METRICS_URL",
    "http://qwen38-flash-next.llm.svc.cluster.local:8000/metrics",
)
VLLM_MODEL_NAME = os.environ.get("SESSION_ROUTER_VLLM_MODEL_NAME", RESIDENT_MODEL)
WAIT_TOLERANCE_SECONDS = float(os.environ.get("SESSION_ROUTER_WAIT_TOLERANCE_S", "5"))
VLLM_POLL_SECONDS = 1.0
VLLM_POLL_TIMEOUT_SECONDS = 0.8
# Sin lectura buena en este tiempo el dato no vale: la válvula de cola no actúa.
VLLM_STALE_SECONDS = 5.0
_vllm_queue = {"waiting": None, "running": None, "since": None, "read_at": None}
_vllm_poller = None
# Filas en vuelo de las OTRAS réplicas (agregado del sidecar menos las de este pod,
# leídas en el mismo ciclo) y cuándo se leyeron.
_sidecar_remote = {"rows": None, "read_at": None}
_sidecar_poller = None

# ── Exentos del desborde (26-09-2026, Dani: "eximir a la compañía del desborde") ──
# Medido el 26-09: el 57 % de las peticiones de 24 h acabaron en Alibaba y el grueso
# lo ponían los roles autónomos de la compañía (company-architect/cto, 1.300-1.700
# peticiones/día cada uno). La válvula los mudaba a Alibaba y el sticky (TTL renovado
# en cada petición) no los devolvía nunca, con el residente ya libre. Un rol que corre
# en segundo plano puede esperar en la cola del Spark; quien no debe esperar es el
# operador interactivo. Campo ADITIVO `overflow_exempt` de /api/model-routing/config:
#   classes — valores de x-claude-class (hoy solo `company`)
#   keys    — alias de virtual key de LiteLLM (opencode-20260630-local, hermes, ...)
# Una petición exenta (a) no pasa por la válvula (ni instant_reject ni la de la
# compañía): encola en el residente; y (b) si trae un binding sticky a Alibaba que no
# es una orden del operador (plan explícito o default_plan=alibaba), se re-vincula al
# residente — así se sueltan las sesiones ya capturadas. NO toca el camino de avería
# (residente no Ready -> rebind_alibaba) ni los fallbacks del Router: exento = no
# desborda por capacidad, no = sin red. Ausente o no-dict = DEFAULT (la compañía
# exenta); una lista vacía guardada desde el panel = nadie exento (= antes del 26-09).
DEFAULT_OVERFLOW_EXEMPT = {"classes": ["company"], "keys": []}


def _sanitize_exempt(raw):
    if not isinstance(raw, dict):
        return {k: list(v) for k, v in DEFAULT_OVERFLOW_EXEMPT.items()}
    out = {}
    for k in DEFAULT_OVERFLOW_EXEMPT:
        v = raw.get(k)
        if isinstance(v, list):
            out[k] = sorted({str(x).strip().lower() for x in v if isinstance(x, str) and x.strip()})
        else:
            out[k] = list(DEFAULT_OVERFLOW_EXEMPT[k])
    return out


def _key_alias(data):
    """Alias de la virtual key que hizo la petición (metadata o litellm_metadata,
    según la ruta — mismo criterio que _claude_class). None si no hay."""
    for meta in (data.get("metadata"), data.get("litellm_metadata")):
        if isinstance(meta, dict) and meta.get("user_api_key_alias"):
            return str(meta["user_api_key_alias"]).strip().lower() or None
    return None


def _overflow_exempt(config, claude_class, key_alias):
    ex = config.get("overflow_exempt")
    if not isinstance(ex, dict):
        ex = DEFAULT_OVERFLOW_EXEMPT
    return bool(
        (claude_class and claude_class in (ex.get("classes") or ()))
        or (key_alias and key_alias in (ex.get("keys") or ()))
    )


# Ambos flags off (default, y estado mientras el panel no exista o no esté
# alcanzable): los mecanismos AUTOMÁTICOS duermen y llamar a
# apply_session_routing es un no-op exacto del comportamiento anterior. Un plan
# EXPLÍCITO por sesión sí aplica con los flags off: es una orden directa del
# operador, no un mecanismo automático.
DEFAULT_CONFIG = {
    "sticky": False,
    "instant_reject": False,
    "local_slots": DEFAULT_LOCAL_SLOTS,
    "default_plan": "local",
    "session_plans": {},
    "company": dict(DEFAULT_COMPANY),
    "alibaba": dict(DEFAULT_ALIBABA),
    "overflow_exempt": {k: list(v) for k, v in DEFAULT_OVERFLOW_EXEMPT.items()},
    # Presupuesto KV (27-09-2026, ADITIVOS): con el panel viejo estos defaults
    # ya valen; la válvula además necesita capacidad descubierta y HASH fresco
    # para actuar, así que estrenar el código no cambia el routing de nadie.
    "kv_budget_pct": DEFAULT_KV_BUDGET_PCT,
    "bot_budget_pct": DEFAULT_BOT_BUDGET_PCT,
    "session_idle_s": DEFAULT_SESSION_IDLE_S,
    "return_max_tokens": DEFAULT_RETURN_MAX_TOKENS,
    "alibaba_return_after_s": DEFAULT_ALIBABA_RETURN_AFTER_S,
    "bot_keys": list(DEFAULT_BOT_KEYS),
}

_config_cache = {"config": dict(DEFAULT_CONFIG), "expires": 0.0}
_config_refresh_task = None
_config_client = None
_redis_client = None
_redis_lock = asyncio.Lock()
_bg_tasks = set()
_warn_state = {}


def _schedule(coro):
    """Lanza una tarea en background sin perder la referencia (un task sin
    referencia puede ser recolectado a medias por el GC de asyncio)."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _warn_throttled(key, mensaje):
    """Warning como mucho 1/min por clave con cuenta acumulada: un fallo
    sistemático (p.ej. valkey saturado bajo carga) no puede inundar el log —
    930 líneas en 40 min midió el QA live del 21-09."""
    now = time.monotonic()
    prev = _warn_state.get(key)
    if prev is None or now - prev[0] >= 60.0:
        log.warning("session_router: %s (x%d)", mensaje, (prev[1] if prev else 0) + 1)
        _warn_state[key] = (now, 0)
    else:
        _warn_state[key] = (prev[0], prev[1] + 1)


def _is_uncensored(name):
    name = str(name or "")
    return name.endswith(UNCENSORED_SUFFIX) or name in UNCENSORED_ALIASES


def _session_id(data):
    """El sid de la sesión cliente, con las mismas fuentes que
    litellm_pre_call_utils usa para litellm_trace_id (x-claude-code-session-id
    nativo del CLI 2.1.x, x-litellm-session-id, metadata.user_id
    `_session_<uuid>`). Verificado en el pod: add_litellm_data_to_request corre
    ANTES del pre_call_hook, así que data["litellm_trace_id"] ya existe."""
    sid = data.get("litellm_trace_id")
    if sid:
        return str(sid)
    md = data.get("metadata") or {}
    sid = md.get("litellm_trace_id")
    if sid:
        return str(sid)
    for source in (data.get("headers"), md.get("headers")):
        headers = source or {}
        for header in ("x-claude-code-session-id", "x-litellm-session-id"):
            value = headers.get(header)
            if value:
                return str(value)
    user_id = md.get("user_id") or ""
    if isinstance(user_id, str) and user_id.startswith("_session_"):
        return user_id
    return None


# Llave de afinidad para clientes SIN id de sesión (23-09-2026). Medido ese día:
# opencode manda 22 peticiones con 22 session_id distintos en 10 min (y hermes igual):
# sin id estable no hay sticky, cada petición se decide suelta y la conversación salta
# entre el local y Alibaba llegando fría a los dos. El sistema y el primer mensaje de
# usuario NO cambian en toda la conversación: su hash (con el cliente) la identifica.
# Las marcas cache_control se ignoran (los clientes las mueven entre turnos).
AFFINITY_CHARS = 20000


def _sin_cache_control(obj):
    if isinstance(obj, dict):
        return {k: _sin_cache_control(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_sin_cache_control(v) for v in obj]
    return obj


def _prefix_affinity_key(data):
    """`pfx-<hash>` estable para toda una conversación sin id, o None."""
    try:
        msgs = data.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return None
        metas = [data.get("metadata"), data.get("litellm_metadata")]
        alias = next((m.get("user_api_key_alias") or m.get("user_api_key_hash")
                      for m in metas if isinstance(m, dict)
                      and (m.get("user_api_key_alias") or m.get("user_api_key_hash"))), "")

        def trozo(x):
            x = _sin_cache_control(x)
            texto = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)
            return texto[:AFFINITY_CHARS]

        partes = [str(alias), trozo(data.get("system") or "")]
        for m in msgs:
            if not isinstance(m, dict):
                continue
            partes.append(f"{m.get('role')}:{trozo(m.get('content'))}")
            if m.get("role") == "user":
                break
        return "pfx-" + hashlib.sha1("\x1f".join(partes).encode("utf-8", "replace")).hexdigest()[:24]
    except Exception:
        return None


def _claude_class(data):
    """La clase de sesión que estampa el wrapper (x-claude-class). Vía real
    verificada contra litellm v1.100.0 (la pineada) y sonda en vivo el 22-09
    — ver plans/company-class-instant-reject-plan.md:
    add_litellm_data_to_request asigna INCONDICIONALMENTE todas las cabeceras
    del request (salvo credenciales, enmascaradas) a data["metadata"]
    ["headers"], y clean_headers guarda las claves con la CAJA DEL CABLE
    (itera sin normalizar): por eso la comparación es case-insensitive por
    clave y el valor se normaliza a minúsculas. data["headers"] (raíz) solo
    existe con forward_client_headers_to_llm_api (apagado en este
    despliegue) y llega remapeada con prefijo x-litellm-; se lee también por
    robustez, igual que en _session_id.

    OJO (23-09-2026, medido en vivo): en las rutas de LITELLM_METADATA_ROUTES
    — `/v1/messages` entre ellas, que es por donde entra TODO Claude Code — el
    mismo add_litellm_data_to_request escribe en data["litellm_metadata"]
    ["headers"], no en data["metadata"]. Sin leer ahí, una sesión de la
    compañía por /v1/messages salía sin clase: ni la exención de la válvula ni
    el interruptor «Fallback Alibaba» la reconocían. Se leen las tres fuentes.

    Fail-open: cualquier forma rara (no-dict, items() roto, None) => None =>
    comportamiento actual; no lanza nunca. La cabecera es falsificable por
    cualquier cliente con key: el único efecto es ENCOLAR en vez de saltar a
    Alibaba bajo saturación (auto-perjuicio), sin escalar privilegios — no
    toca sellado, admisión ni planes explícitos."""
    # CONTRACT: dgx.claude.class-header.v1
    try:
        sources = [
            data.get("headers"),
            (data.get("metadata") or {}).get("headers"),
            (data.get("litellm_metadata") or {}).get("headers"),
        ]
    except AttributeError:
        return None
    for source in sources:
        if not isinstance(source, dict):
            continue
        try:
            for key, value in source.items():
                if isinstance(key, str) and key.lower() == CLASS_HEADER and value:
                    return str(value).strip().lower() or None
        except (AttributeError, TypeError):
            continue
    return None


def _sanitize_account_weights(raw):
    """`alibaba_account_weights` (29-09-2026, ADITIVO): {"k1": peso, "k2": peso} con pesos
    finitos >= 0. Cualquier otra forma => {} (= reparto uniforme de siempre)."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and _ACCOUNT_KEY_RE.fullmatch(k)):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            return {}
        out[k] = float(v)
    return out if sum(out.values()) > 0 else {}


def _sanitize(raw):
    """El panel manda el deseado; nada de confiar en tipos. Campo inválido =>
    default, nunca excepción (el panel ya proyecta estricto, esto es la segunda
    red del consumidor fail-open)."""
    plans = raw.get("session_plans")
    config = {
        "sticky": bool(raw.get("sticky", DEFAULT_CONFIG["sticky"])),
        "instant_reject": bool(raw.get("instant_reject", DEFAULT_CONFIG["instant_reject"])),
        "local_slots": DEFAULT_LOCAL_SLOTS,
        "default_plan": raw.get("default_plan") if raw.get("default_plan") in PLANES else "local",
        "session_plans": (
            {str(k): v for k, v in plans.items() if v in PLANES}
            if isinstance(plans, dict)
            else {}
        ),
    }
    # Solo un false bool apaga un interruptor de la compañía: ausente, null o basura = hoy.
    company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    config["company"] = {k: company.get(k) is not False for k in DEFAULT_COMPANY}
    alibaba = raw.get("alibaba") if isinstance(raw.get("alibaba"), dict) else {}
    config["alibaba"] = {k: alibaba.get(k) is not False for k in DEFAULT_ALIBABA}
    config["overflow_exempt"] = _sanitize_exempt(raw.get("overflow_exempt"))
    slots = raw.get("local_slots")
    if isinstance(slots, int) and not isinstance(slots, bool) and 0 <= slots <= LOCAL_SLOTS_CAP:
        config["local_slots"] = slots
    # Presupuesto KV (27-09-2026, ADITIVOS): campo inválido o ausente => default,
    # nunca excepción (segunda red del consumidor fail-open, como el resto).
    for key, default, lo, hi in (
        ("kv_budget_pct", DEFAULT_KV_BUDGET_PCT, 1, 100),
        ("bot_budget_pct", DEFAULT_BOT_BUDGET_PCT, 1, 100),
        ("session_idle_s", DEFAULT_SESSION_IDLE_S, 30, 86400),
        ("return_max_tokens", DEFAULT_RETURN_MAX_TOKENS, 1000, 1000000),
        ("alibaba_return_after_s", DEFAULT_ALIBABA_RETURN_AFTER_S, 60, 86400),
    ):
        value = raw.get(key)
        config[key] = (
            value if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi
            else default
        )
    config["alibaba_account_weights"] = _sanitize_account_weights(raw.get("alibaba_account_weights"))
    bots = raw.get("bot_keys")
    config["bot_keys"] = (
        sorted({str(x).strip().lower() for x in bots if isinstance(x, str) and x.strip()})
        if isinstance(bots, list) else list(DEFAULT_BOT_KEYS)
    )
    return config


async def _config():
    """Stale-while-revalidate: el camino de la petición NO hace NINGUNA I/O de
    config — devuelve la caché al instante (defaults seguros en frío) y, si está
    vencida, lanza el refresco en background single-flight. Propagación de un
    cambio del panel: TTL 5 s + ~0.3 s del refresco (C8 ≤10 s sigue holgado).
    El patrón anterior (fetch síncrono con tope de 100 ms) era correcto en
    presupuesto pero ciego en la práctica: el backend tarda 130-300 ms por el
    subprocess kubectl y TODO fetch moría en ReadTimeout — el fail-open tapaba
    el panel entero."""
    global _config_refresh_task
    if time.monotonic() >= _config_cache["expires"]:
        if _config_refresh_task is None or _config_refresh_task.done():
            _config_refresh_task = asyncio.create_task(_refresh_config())
    return _config_cache["config"]


async def _refresh_config():
    """Refresco en background: un task a la vez (single-flight por task.done()),
    cliente persistente con keep-alive, NUNCA lanza (un task muerto no debe
    arrastrar el hook). Renueva el TTL incluso en fallo: sin eso, cada petición
    relanzaría el intento y habría una tormenta de tasks contra el backend."""
    global _config_client
    try:
        if _config_client is None or _config_client.is_closed:
            _config_client = httpx.AsyncClient(
                timeout=httpx.Timeout(CONFIG_REFRESH_TIMEOUT_SECONDS))
        response = await _config_client.get(CONFIG_URL)
        response.raise_for_status()
        candidate = response.json()
        if isinstance(candidate, dict):
            _config_cache["config"] = _sanitize(candidate)
        # candidato no-dict: se conserva el último bueno (segunda red de _sanitize)
    except Exception as exc:
        log.warning(
            "session_router: config de routing no alcanzable (%s); sigo con el último conocido",
            exc,
        )
    _config_cache["expires"] = time.monotonic() + CONFIG_TTL_SECONDS


def _redis_url():
    """requirepass obligatorio (mandato A): la contraseña llega por
    ExternalSecret -> env SESSION_ROUTER_REDIS_PASSWORD y se inyecta en la URL.
    Nunca se loguea."""
    url = REDIS_URL
    password = os.environ.get("SESSION_ROUTER_REDIS_PASSWORD") or ""
    if password:
        parts = urlsplit(url)
        netloc = f":{quote(password, safe='')}@{parts.hostname or 'localhost'}"
        if parts.port:
            netloc += f":{parts.port}"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


async def _redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    async with _redis_lock:
        if _redis_client is None:
            try:
                import redis.asyncio as aioredis
                _redis_client = aioredis.from_url(
                    _redis_url(),
                    # Presupuesto de SOCKET holgado (2 s): medido en vivo el 21-09,
                    # DNS=82 ms y conectar+AUTH en frío ≈ 220 ms — con 100 ms de
                    # socket_timeout NINGUNA conexión fría llegaba nunca, ni
                    # siquiera la escritura en background (el wait_for de 2 s no
                    # aplicaba: moría antes en el socket). El presupuesto de 100 ms
                    # del camino de la petición (mandato 10) lo impone el
                    # asyncio.wait_for de _sticky_get, no el socket; la operación
                    # en curso se cancela y la conexión se descarta (fail-open).
                    socket_timeout=STICKY_WRITE_TIMEOUT_SECONDS,
                    socket_connect_timeout=STICKY_WRITE_TIMEOUT_SECONDS,
                    health_check_interval=REDIS_HEALTH_CHECK_INTERVAL,
                    decode_responses=True,
                )
                # Warmup en background: la primera conexión (DNS+TCP+AUTH) se paga
                # fuera del camino de la petición; sin esto, el primer GET sticky
                # de cada pod consumía su presupuesto de 100 ms en el handshake.
                _schedule(_redis_warmup(_redis_client))
            except Exception as exc:
                log.warning("session_router: redis no disponible (%s); fail-open", exc)
                return None
    return _redis_client


async def _redis_warmup(client):
    try:
        await asyncio.wait_for(client.ping(), timeout=STICKY_WRITE_TIMEOUT_SECONDS)
    except Exception as exc:
        _warn_throttled("redis_warmup", f"warmup de redis falló ({exc.__class__.__name__})")


async def _sticky_get(sid):
    try:
        client = await _redis()
        if client is None:
            return None
        value = await asyncio.wait_for(
            client.get(f"{STICKY_KEY_PREFIX}{sid}"),
            timeout=REDIS_OP_TIMEOUT_SECONDS,
        )
        return value if value in ("local", "alibaba") else None
    except Exception as exc:
        # Fail-open (sesión tratada como fresca) pero ya no EN SILENCIO: el QA
        # live del 21-09 mostró sesiones ligadas a alibaba re-evaluadas como
        # frescas porque este timeout no dejaba rastro.
        _warn_throttled("sticky_get", f"sticky_get falló ({exc.__class__.__name__}); trato la sesión como fresca")
        return None


async def _sticky_set(sid, plan):
    """Fire-and-forget: programa la escritura en background y devuelve el
    control AL INSTANTE. Fix del defecto 1 del QA live (21-09): con el
    presupuesto de 100 ms en el camino de la petición, ~99% de las escrituras
    morían bajo carga (930+ fallos en 40 min, 6 claves escritas, 0 de plan
    local) — el sticky quedaba inoperativo justo cuando más falta hacía. La
    pérdida puntual se tolera por diseño: la siguiente petición re-intenta el
    vínculo."""
    _schedule(_sticky_set_bg(sid, plan))


def _sticky_ttl(plan):
    """TTL de inactividad del vínculo según su destino (ver STICKY_TTL_*)."""
    return STICKY_TTL_ALIBABA_SECONDS if plan == "alibaba" else STICKY_TTL_LOCAL_SECONDS


async def _sticky_set_bg(sid, plan):
    try:
        client = await _redis()
        if client is None:
            return
        await asyncio.wait_for(
            client.set(f"{STICKY_KEY_PREFIX}{sid}", plan, ex=_sticky_ttl(plan)),
            timeout=STICKY_WRITE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _warn_throttled("sticky_set", f"sticky_set falló ({exc.__class__.__name__}); fail-open")


def _parse_vllm_gauges(text, model_name=None):
    """(waiting, running) del texto Prometheus de vLLM para `model_name`, sumando
    las series que haya (una por engine). (None, None) si no aparece ninguna."""
    model_name = model_name or VLLM_MODEL_NAME
    marca = f'model_name="{model_name}"'
    waiting = running = None
    for line in (text or "").splitlines():
        if line.startswith("#") or marca not in line:
            continue
        nombre = line.split("{", 1)[0]
        if nombre not in ("vllm:num_requests_waiting", "vllm:num_requests_running"):
            continue
        try:
            valor = float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
        if nombre == "vllm:num_requests_waiting":
            waiting = (waiting or 0.0) + valor
        else:
            running = (running or 0.0) + valor
    return waiting, running


def _parse_cache_capacity(text):
    """Capacidad KV total en TOKENS desde vllm:cache_config_info: un gauge-INFO
    (valor 1.0) cuya capacidad vive en los LABELLES. Medido en el head del
    residente el 28-09: la línea lleva `kv_cache_size_tokens="2775211"` — el
    número EXACTO del log de arranque «GPU KV cache size: 2,775,211 tokens» —
    y ESE manda. El producto num_gpu_blocks × block_size (1895 × 1664 =
    3.15M) SOBREESTIMA ~14 % en el modelo híbrido: el estado Mamba se come una
    parte de la cache, así que el producto queda como fallback para vLLM viejos
    sin la etiqueta. None si la línea no está (motor sin arrancar, vLLM cambió
    el métrico, o num_gpu_blocks="None" antes de la profilaxis): la válvula de
    presupuesto se APAGA (fail-open). Si hubiera varias líneas (varios engines)
    manda la mayor."""
    best = None
    for line in (text or "").splitlines():
        if line.startswith("#") or "vllm:cache_config_info" not in line:
            continue
        m_exact = re.search(r'kv_cache_size_tokens="(\d+)"', line)
        if m_exact:
            tokens = int(m_exact.group(1))
        else:
            # `(?<![a-z_])` porque la línea híbrida trae también
            # mamba_block_size="16": sin el guard, re.search lo pescaría.
            m_blocks = re.search(r'num_gpu_blocks="(\d+)"', line)
            m_size = re.search(r'(?<![a-z_])block_size="(\d+)"', line)
            if not (m_blocks and m_size):
                continue
            try:
                tokens = int(m_blocks.group(1)) * int(m_size.group(1))
            except ValueError:
                continue
        if tokens > 0 and (best is None or tokens > best):
            best = tokens
    return best


def _note_vllm_sample(waiting, running, now=None):
    """Registra una lectura. `since` = desde cuándo la cola NO se ha vaciado."""
    now = time.monotonic() if now is None else now
    _vllm_queue["waiting"] = waiting
    _vllm_queue["running"] = running
    _vllm_queue["read_at"] = now
    if waiting and waiting > 0:
        if _vllm_queue["since"] is None:
            _vllm_queue["since"] = now
    else:
        _vllm_queue["since"] = None


async def _poll_vllm_forever():
    client = httpx.AsyncClient(timeout=httpx.Timeout(VLLM_POLL_TIMEOUT_SECONDS))
    while True:
        try:
            response = await client.get(VLLM_METRICS_URL)
            response.raise_for_status()
            waiting, running = _parse_vllm_gauges(response.text)
            if waiting is not None:
                _note_vllm_sample(waiting, running)
            # Capacidad KV (27-09): MISMA lectura que la cola, cero I/O extra.
            # Último-bueno: la cache solo cambia al reiniciar el motor, y el
            # siguiente sondeo bueno la sustituye; sin sondeo la válvula sigue
            # decidiendo con el último valor conocido.
            capacity = _parse_cache_capacity(response.text)
            if capacity:
                _kv_capacity["tokens"] = capacity
                _kv_capacity["read_at"] = time.monotonic()
        except Exception as exc:
            _warn_throttled("vllm_poll", f"sondeo de la cola de vLLM falló ({exc.__class__.__name__}); sin válvula de cola")
        await asyncio.sleep(VLLM_POLL_SECONDS)


def _ensure_vllm_poller():
    """Arranca el sondeo la primera vez que hace falta (y lo rearranca si murió)."""
    global _vllm_poller
    if _vllm_poller is None or _vllm_poller.done():
        _vllm_poller = asyncio.create_task(_poll_vllm_forever())


# ── Presupuesto KV: HASH de sesiones (27-09-2026) ───────────────────────────


def _estimate_prompt_tokens(data, tracker=None):
    """Tokens del prompt, ESTIMADOS: caracteres / chars_per_token. El número
    real lo manda vLLM al acabar el prefill — tarde para decidir dónde se
    sirve. Mismo criterio que prompt_chars() del tracker
    (active_request_tracking.py, duplicado a propósito para no acoplar el
    import): recorre el body sin serializarlo, no cuenta el base64 de los
    adjuntos (sus tokens no salen de sus caracteres) e ignora cache_control.
    chars_per_token lo aporta el tracker APRENDIDO de las peticiones reales
    (arranca en 3,5); sin tracker o con dato roto, 3,5. Un turno de Claude
    Code son ~300 KB: la misma vuelta que el tracker ya da por petición,
    sub-milisegundos, dentro del presupuesto (mandato 10). Cualquier forma
    rara => None => la válvula no bloquea (fail-open)."""
    try:
        if not isinstance(data, dict):
            return None
        total = 0
        stack = [data.get(key) for key in (
            "system", "instructions", "messages", "input", "prompt", "tools")]
        media = frozenset(("image", "image_url", "input_image", "input_audio",
                           "document", "file"))
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                if node.startswith("data:") and len(node) > 1024:
                    continue
                total += len(node)
            elif isinstance(node, dict):
                if node.get("type") in media:
                    continue
                for key, value in node.items():
                    if key == "cache_control":
                        continue
                    total += len(key)
                    stack.append(value)
            elif isinstance(node, (list, tuple)):
                stack.extend(node)
        if total <= 0:
            return None
        cpc = getattr(tracker, "chars_per_token", None) or FALLBACK_CHARS_PER_TOKEN
        return int(total / float(cpc)) + 1
    except Exception:
        return None


def _sessions_aggregate(raw, now=None, idle_s=None, prune_s=None):
    """De un HGETALL {sid: json{t,ts}}: (suma de las sesiones VIVAS, nº vivas,
    campos a podar). Viva = actividad en los últimos idle_s (precedencia 4b).
    Se poda lo muerto hace más de prune_s — por encima de cualquier TTL
    sticky: la entrada ya no sirve (la sesión que vuelva se re-anota como
    nueva o como fresh). La poda por HDEL es idempotente entre réplicas; la
    carrera (otro pod renueva el campo entre el HGETALL y el HDEL) a lo sumo
    borra una entrada viva que la siguiente petición reescribe: subestima
    brevemente, la dirección fail-open de siempre."""
    now = time.time() if now is None else now
    idle_s = DEFAULT_SESSION_IDLE_S if idle_s is None else idle_s
    if prune_s is None:
        prune_s = max(STICKY_TTL_ALIBABA_SECONDS, STICKY_TTL_LOCAL_SECONDS, idle_s) + 60
    total = 0
    n = 0
    stale = []
    for sid, value in (raw or {}).items():
        try:
            entry = json.loads(value)
            ts = float(entry["ts"])
            tokens = int(entry["t"])
        except (TypeError, ValueError, KeyError):
            stale.append(str(sid))
            continue
        if now - ts > prune_s:
            stale.append(str(sid))
            continue
        if now - ts <= idle_s:
            total += tokens
            n += 1
    return total, n, stale


async def _sessions_write_bg(sid, tokens):
    try:
        client = await _redis()
        if client is None:
            return
        await asyncio.wait_for(
            client.hset(SESSIONS_HASH_KEY, sid,
                        json.dumps({"t": int(tokens), "ts": time.time()})),
            timeout=STICKY_WRITE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _warn_throttled("sessions_write", f"registro de sesión en el HASH falló ({exc.__class__.__name__}); fail-open")


def _sessions_write(sid, tokens):
    """Fire-and-forget (como _sticky_set): el camino de la petición no espera
    a Valkey. Solo peticiones servidas por el residente: son las que ocupan
    la KV cache local."""
    if sid and tokens:
        _schedule(_sessions_write_bg(sid, tokens))


async def _poll_sessions_forever():
    while True:
        try:
            client = await _redis()
            if client is not None:
                config = await _config()
                raw = await asyncio.wait_for(
                    client.hgetall(SESSIONS_HASH_KEY),
                    timeout=KV_POLL_TIMEOUT_SECONDS,
                )
                total, n, stale = _sessions_aggregate(
                    raw, idle_s=_cfg_int(config, "session_idle_s", DEFAULT_SESSION_IDLE_S))
                if stale:
                    try:
                        await asyncio.wait_for(
                            client.hdel(SESSIONS_HASH_KEY, *stale),
                            timeout=KV_POLL_TIMEOUT_SECONDS,
                        )
                    except Exception as exc:
                        _warn_throttled("sessions_prune", f"poda del HASH falló ({exc.__class__.__name__}); sigue viva la última suma")
                _kv_sessions["total"] = total
                _kv_sessions["n"] = n
                _kv_sessions["read_at"] = time.monotonic()
        except Exception as exc:
            _warn_throttled("sessions_poll", f"sondeo del HASH de sesiones falló ({exc.__class__.__name__}); válvula KV apagada")
        await asyncio.sleep(KV_POLL_SECONDS)


def _ensure_sessions_poller():
    """Arranca el sondeo del HASH la primera vez que hace falta (y lo rearranca si murió)."""
    global _kv_poller
    if _kv_poller is None or _kv_poller.done():
        _kv_poller = asyncio.create_task(_poll_sessions_forever())


# ── Nacimiento en Alibaba (regla de retorno 4c) ─────────────────────────────


async def _albind_touch_bg(sid):
    try:
        client = await _redis()
        if client is None:
            return

        async def _touch():
            key = f"{ALBIND_KEY_PREFIX}{sid}"
            # SETNX: el primer vínculo manda (se mide DESDE el nacimiento, no
            # desde la última actividad). Si la key ya existe solo se empuja
            # su TTL (EXPIRE, el valor NO se toca): una sesión ligada a
            # Alibaba más de 24 h no puede quedarse sin reloj porque la key
            # venza.
            if not await client.set(key, time.time(), ex=ALBIND_TTL_SECONDS, nx=True):
                await client.expire(key, ALBIND_TTL_SECONDS)

        await asyncio.wait_for(_touch(), timeout=STICKY_WRITE_TIMEOUT_SECONDS)
    except Exception as exc:
        _warn_throttled("albind_touch", f"registro de nacimiento en Alibaba falló ({exc.__class__.__name__}); fail-open")


def _albind_touch(sid):
    if sid:
        _schedule(_albind_touch_bg(sid))


async def _albind_clear_bg(sid):
    try:
        client = await _redis()
        if client is None:
            return
        await asyncio.wait_for(
            client.delete(f"{ALBIND_KEY_PREFIX}{sid}"),
            timeout=STICKY_WRITE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _warn_throttled("albind_clear", f"borrado de nacimiento en Alibaba falló ({exc.__class__.__name__}); fail-open")


def _albind_clear(sid):
    """Al volver a local la sesión deja de estar «nacida en Alibaba»: la
    próxima mudanza mide el reloj desde cero."""
    if sid:
        _schedule(_albind_clear_bg(sid))


async def _albind_get(sid):
    """Época de nacimiento en Alibaba o None. Va EN el camino de la petición
    (la regla de retorno decide ahora) con el presupuesto de 100 ms de
    _sticky_get: sin dato, el retorno por tiempo NO actúa y la sesión sigue
    en Alibaba = comportamiento de hoy (fail-open)."""
    try:
        client = await _redis()
        if client is None:
            return None
        value = await asyncio.wait_for(
            client.get(f"{ALBIND_KEY_PREFIX}{sid}"),
            timeout=REDIS_OP_TIMEOUT_SECONDS,
        )
        return float(value) if value else None
    except Exception as exc:
        _warn_throttled("albind_get", f"albind_get falló ({exc.__class__.__name__}); sin retorno por tiempo")
        return None


async def _poll_sidecar_forever():
    headers = {}
    master_key = os.environ.get("LITELLM_MASTER_KEY") or ""
    if master_key:
        headers["Authorization"] = f"Bearer {master_key}"
    client = httpx.AsyncClient(timeout=SIDECAR_POLL_TIMEOUT_SECONDS)
    while True:
        try:
            aggregate = await client.get(SIDECAR_URL, headers=headers)
            aggregate.raise_for_status()
            own = await client.get(SIDECAR_LOCAL_URL, headers=headers)
            own.raise_for_status()
            own_ids = {
                row.get("request_id")
                for row in ((own.json() or {}).get("active") or [])
                if isinstance(row, dict)
            }
            _sidecar_remote["rows"] = [
                row
                for row in ((aggregate.json() or {}).get("active") or [])
                if isinstance(row, dict) and row.get("request_id") not in own_ids
            ]
            _sidecar_remote["read_at"] = time.monotonic()
        except Exception as exc:
            _warn_throttled("sidecar_poll", f"sondeo del sidecar de peticiones en vuelo falló ({exc.__class__.__name__}); solo cuento este pod")
        await asyncio.sleep(SIDECAR_POLL_SECONDS)


def _ensure_sidecar_poller():
    """Arranca el sondeo del sidecar la primera vez que hace falta (y lo rearranca si murió)."""
    global _sidecar_poller
    if _sidecar_poller is None or _sidecar_poller.done():
        _sidecar_poller = asyncio.create_task(_poll_sidecar_forever())


def _queue_stuck(now=None):
    """True si la cola de vLLM lleva >= WAIT_TOLERANCE_SECONDS sin vaciarse; False si
    no; None si no hay lectura fresca (=> la válvula de cola no actúa)."""
    now = time.monotonic() if now is None else now
    read_at = _vllm_queue["read_at"]
    if read_at is None or now - read_at > VLLM_STALE_SECONDS:
        return None
    since = _vllm_queue["since"]
    return since is not None and now - since >= WAIT_TOLERANCE_SECONDS


async def _inflight_resident(tracker):
    """Peticiones en vuelo al residente (mandato 1): este pod por el tracker
    in-process (exacto) + las OTRAS réplicas por la última lectura del sondeo del
    sidecar :4001 (agregado menos /local, ver _poll_sidecar_forever), deduplicando
    por request_id. None => contador
    desconocido => la válvula NO actúa (fail-open hacia admitir local, con el
    semaphore duro de 8 del Router detrás protegiendo igual)."""
    local_ids = set()
    local_n = 0
    try:
        snapshot = tracker.snapshot() if tracker is not None else {}
        for rid, row in (snapshot or {}).items():
            if isinstance(row, dict) and row.get("model") in RESIDENT_FAMILY:
                local_n += 1
                local_ids.add(rid)
    except Exception as exc:
        log.warning("session_router: tracker snapshot falló (%s); sin válvula", exc)
        return None
    remote_n = 0
    _ensure_sidecar_poller()
    rows = _sidecar_remote["rows"]
    read_at = _sidecar_remote["read_at"]
    if rows is not None and read_at is not None and time.monotonic() - read_at <= SIDECAR_STALE_SECONDS:
        for row in rows:
            if row.get("model") in RESIDENT_FAMILY and row.get("request_id") not in local_ids:
                remote_n += 1
    # Sin lectura fresca (sidecar mudo, par caído, recién arrancado): solo lo local,
    # que subestima => admite de más en local, la dirección fail-open. El semaphore
    # 8 por pod sigue topando.
    return local_n + remote_n


def _is_bot_session(config, claude_class, key_alias):
    """Prioridad (27-09-2026): el interactivo va primero. Bot = clase company
    (los roles autónomos de la compañía corren en segundo plano) o alias de
    virtual key en bot_keys (hermes, aurora-rca). claude-cli/opencode/
    open-webui NO son bot. Sin key alias o config vieja: no bot (fail-open
    hacia local). La clase es falsificable (dgx.claude.class-header.v1) y el
    alias lo pone la key: el único efecto de mentir es que la sesión se
    admita con el umbral alto en vez del bajo — auto-perjuicio, sin escalar
    privilegios."""
    if claude_class == COMPANY_CLASS:
        return True
    if not key_alias:
        return False
    bots = config.get("bot_keys")
    if not isinstance(bots, (list, tuple, set)):
        bots = DEFAULT_BOT_KEYS
    return key_alias in bots


def _kv_budget_state(config, est, is_bot):
    """(limite_tokens, capacidad, suma_viva) o None si la válvula no puede
    decidir: capacidad desconocida (el residente no expone
    cache_config_info — p.ej. tras un cambio de modelo aún sin sondeo), HASH
    sin lectura fresca, o prompt sin estimar. El camino de la petición NO
    hace I/O: lee los cachés que escriben los sondeos. None = APAGADA =
    comportamiento actual (fail-open). El límite es un PORCENTAJE de la
    capacidad descubierta, nunca un número fijo de tokens: al cambiar el
    residente cambia la cache (27-09: 2.775.211 tokens con qwen38-flash-next
    TP=2)."""
    global _kv_no_capacity_logged
    capacity = _kv_capacity["tokens"]
    if capacity is None:
        if not _kv_no_capacity_logged:
            _kv_no_capacity_logged = True
            log.warning(
                "session_router: capacidad KV desconocida (vllm:cache_config_info no visto); "
                "válvula de presupuesto APAGADA (fail-open)")
        return None
    total = _kv_sessions["total"]
    read_at = _kv_sessions["read_at"]
    if total is None or read_at is None or time.monotonic() - read_at > KV_STALE_SECONDS:
        return None
    if est is None:
        return None
    pct = _cfg_int(
        config, "bot_budget_pct" if is_bot else "kv_budget_pct",
        DEFAULT_BOT_BUDGET_PCT if is_bot else DEFAULT_KV_BUDGET_PCT,
    )
    return capacity * pct / 100.0, capacity, total


def _kv_blocks(config, est, is_bot):
    """True => una sesión NUEVA no cabe en el residente: nace en Alibaba
    (precedencia 4b). Nunca expulsa una sesión ya ligada a local: eso es
    decisión del llamador (solo llama con fresh)."""
    state = _kv_budget_state(config, est, is_bot)
    if state is None:
        return False
    limit, _capacity, total = state
    return est + total > limit


async def _may_return_local(config, sid, est, is_bot):
    """Regla de retorno (precedencia 4c) para una sesión ya ligada a Alibaba:
    motivo de retorno o None (sigue en Alibaba). Vuelve si (a) compactó — su
    prompt actual <= return_max_tokens — y cabe, o (c) nació en Alibaba hace
    >= alibaba_return_after_s y cabe. «Cabe» = prompt actual + sesiones
    locales vivas <= umbral; sin datos de presupuesto se admite el retorno
    (fail-open hacia local, la dirección de todo el módulo). El (b) «sesión
    nueva» no vive aquí: una sesión sin binding entra fresh por 4b."""
    state = _kv_budget_state(config, est, is_bot)
    if state is not None and est is not None and est + state[2] > state[0]:
        return None
    return_max = _cfg_int(config, "return_max_tokens", DEFAULT_RETURN_MAX_TOKENS)
    if est is not None and est <= return_max:
        return "compactada"
    after_s = _cfg_int(config, "alibaba_return_after_s", DEFAULT_ALIBABA_RETURN_AFTER_S)
    born = await _albind_get(sid)
    if born is not None and time.time() - born >= after_s:
        return "temporizada"
    return None


def _group_in_cooldown(model_name):
    """True si TODO el grupo está en cooldown; False si queda algún deployment
    utilizable; None si no se pudo saber (=> degradar, mandato 5). API pública
    del Router, en proceso y síncrona; lazy import para no acoplar el arranque."""
    try:
        from litellm.proxy.proxy_server import llm_router
        if llm_router is None:
            # El defecto 2 del QA live (default_plan=alibaba sin reescritura ni
            # rastro) pasó por caminos mudos como este: ahora dicen por qué.
            _warn_throttled("cooldown_router", "llm_router sin inicializar; overflow degradado")
            return None
        model_ids = llm_router.get_model_ids(model_name=model_name)
        if not model_ids:
            _warn_throttled(
                "cooldown_ids_" + str(model_name),
                f"get_model_ids vacío para {model_name!r}; overflow degradado")
            return None
        cooling = llm_router.cooldown_cache.get_active_cooldowns(model_ids, None)
        return len(cooling) >= len(model_ids)
    except Exception as exc:
        log.warning("session_router: consulta de cooldown falló (%s); degrado", exc)
        return None


def _overflow_ok():
    """¿Puede este hook enviar tráfico a Alibaba AHORA? Solo con respuesta
    inequívoca: cooldown activo o desconocido => False (no enrutar, no pegar)."""
    return _group_in_cooldown(OVERFLOW_MODEL) is False


def _rewrite(data, sid, reason):
    """Residente -> Alibaba. Espejo del desvío de visión: se limpian api_base/
    api_key (el grupo de Alibaba lleva los suyos en el model_list) y se marcan
    el modelo original/resuelto + el marcador de reroute (mandato 3)."""
    original = data.get("model")
    data["model"] = OVERFLOW_MODEL
    data.pop("api_base", None)
    data.pop("api_key", None)
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        data["metadata"] = metadata
    metadata["_session_routing_rerouted"] = True
    metadata["_session_routing_reason"] = reason
    metadata["_router_original_model"] = original
    metadata["_router_resolved_model"] = OVERFLOW_MODEL
    # WARNING: el proceso proxy filtra los INFO de este logger (medido 21-09);
    # una reescritura es siempre notable y tiene que verse en el log vivo.
    log.warning(
        "session_router: %r -> %r (razon=%s, sid=%s)",
        original, OVERFLOW_MODEL, reason, sid or "-",
    )


# ── Afinidad de sesion por CUENTA de Alibaba (26-09-2026, rehecha 28-09) ──
# Cada grupo `alibaba-*` tiene dos deployments (dos planes Team). El cache
# de contexto de Model Studio es POR CUENTA: partir una conversacion entre
# las dos paga el prefijo entero en cada salto. Desde el 28-09 el filtro es
# PROPIO (alibaba_account_filter, mas abajo: un pin sesion -> cuenta para
# todos los grupos); el session_affinity nativo por grupo quedo retirado.
# Este modulo:
#   (a) estampa metadata["session_id"] con el sid PROPIO del hook en toda
#       peticion que va a un grupo `alibaba-*`. El id que manda el cliente
#       no vale — se PISA deliberadamente (AFFINITY_CHARS: opencode manda
#       22 distintos por conversacion). Es la llave del pin de cuenta.
#   (b) monta una vez la Valkey del namespace sobre el DualCache del
#       Router (cooldowns compartidos entre replicas).
#   (c) activa la cuenta 2 SOLO si existe: ensure_alibaba_key2_active
#       sube los -k2 de order 2 a 1 en memoria cuando
#       DASHSCOPE_API_KEY_2 esta en el entorno. Sin la key RETIRA los -k2
#       del Router y no estampa sid (la afinidad corre antes que el filtro
#       de order: con un -k2 sin key cargado, una sesion nacida en un
#       cooldown del -k1 quedaba clavada a un 401). Si este hook no carga
#       (el fallo peor), el order: 2 del YAML sigue siendo la inercia:
#       sembrar + rollout = activado, sin paso manual.
# Fail-open: sin sid no se filtra (shuffle puro); sin Valkey la cuenta de una
# sesion nueva sigue siendo la misma en los dos pods (eleccion determinista).

# CONTRACT: dgx.session-router.deployment-affinity-key.v1 (DEPRECATED 28-09)
# (ancla: deployment_affinity:v1:) Solo se LEE, para sembrar la cuenta de las
# sesiones que ya tenian pin nativo por grupo; nadie las escribe ya.

_alibaba_key2_checked = False
_affinity_redis_tried = False


def ensure_alibaba_key2_active():
    """Una vez por proceso, dos estados:

    - Con DASHSCOPE_API_KEY_2: sube los diez -k2 de order 2 a order 1 EN
      MEMORIA (el minimo de orders las incluye en el reparto; la afinidad
      por sesion decide cual).
    - Sin ella: RETIRA los -k2 del Router (delete_deployment). Motivo,
      verificado en la fuente v1.100.0: el filtro de afinidad corre ANTES
      que el de `order` (router.py async_get_healthy_deployments) y el pin
      se reclama en el pre-call. Una sesion nueva que naciera durante un
      cooldown del -k1 quedaba clavada al -k2 SIN key: 401 en cada vuelta,
      y el pin se renovaba solo (keepalive) mientras la sesion viviera.
      Sin -k2 cargado, un cooldown del -k1 sigue la cadena de fallback
      normal en vez de estrellarse contra un 401 seguro.

    Si este hook no carga, el order: 2 del YAML sigue siendo la inercia.
    La key llega por env del pod: sembrar en 1Password + rollout restart
    = activado, sin tocar git."""
    global _alibaba_key2_checked
    if _alibaba_key2_checked:
        return
    _alibaba_key2_checked = True
    con_key = bool(os.environ.get("DASHSCOPE_API_KEY_2"))
    try:
        from litellm.proxy.proxy_server import llm_router
        if llm_router is None:
            _alibaba_key2_checked = False  # router aun sin montar: reintentar
            return
        k2 = []
        for entry in list(getattr(llm_router, "model_list", [])):
            if isinstance(entry, dict):
                info = entry.get("model_info") or {}
                params = entry.get("litellm_params")
            else:
                info = getattr(entry, "model_info", None)
                params = getattr(entry, "litellm_params", None)
            info_id = info.get("id") if isinstance(info, dict) else getattr(info, "id", None)
            if isinstance(info_id, str) and info_id.endswith("-k2"):
                k2.append((info_id, params))
        if con_key:
            for _, params in k2:
                if isinstance(params, dict):
                    params["order"] = 1
                elif params is not None:
                    params.order = 1
            log.warning("session_router: DASHSCOPE_API_KEY_2 presente -> %s deployments -k2 en el reparto (order 1)", len(k2))
        else:
            retirados = sum(1 for info_id, _ in k2 if llm_router.delete_deployment(id=info_id) is not None)
            log.warning("session_router: sin DASHSCOPE_API_KEY_2 -> %s deployments -k2 retirados del Router", retirados)
    except Exception as exc:
        _warn_throttled("alibaba_k2", f"gestion -k2 fallida ({exc.__class__.__name__}); queda el order 2 del YAML")


def alibaba_key2_disponible():
    return bool(os.environ.get("DASHSCOPE_API_KEY_2"))


def ensure_affinity_redis():
    """Una vez por proceso: si el DualCache del Router no tiene tier de
    redis, montar la Valkey del namespace (misma URL y password que el
    sticky). No se reintenta: el precio de fallar es afinidad por pod, no
    error. `_update_redis_cache` solo escribe si el tier esta vacio, asi
    que dos replicas del hook montando a la vez no se pisan."""
    global _affinity_redis_tried
    if _affinity_redis_tried:
        return
    _affinity_redis_tried = True
    try:
        from litellm.proxy.proxy_server import llm_router
        if llm_router is None or llm_router.cache.redis_cache is not None:
            return
        from litellm.caching.redis_cache import RedisCache
        parts = urlsplit(REDIS_URL)
        cache = RedisCache(
            host=parts.hostname or "localhost",
            port=parts.port or 6379,
            password=os.environ.get("SESSION_ROUTER_REDIS_PASSWORD") or None,
            db=int((parts.path or "/0").lstrip("/") or 0),
            # El claim y la lectura del pin van EN el camino de la peticion
            # (litellm los espera): tope duro y corto, Valkey colgada no
            # puede estirar un chat. 5 s era el default de RedisCache.
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
        llm_router._update_redis_cache(cache=cache)
        log.warning("session_router: tier redis montado en el DualCache del Router (afinidad cross-pod)")
    except Exception as exc:
        _warn_throttled("affinity_redis", f"tier redis no montado ({exc.__class__.__name__}); pines por pod")


def stamp_alibaba_session_affinity(data):
    """metadata["session_id"] = sid del hook para peticiones `alibaba-*`
    (incluida la reescrita residente->Alibaba, que ya ve el nombre final).
    Devuelve el sid estampado o None. Sin sid, o sin cuenta 2, no se toca nada."""
    ensure_alibaba_key2_active()
    if not str(data.get("model") or "").startswith(ALIBABA_PREFIX):
        return None
    # Con UNA cuenta la afinidad no aporta nada y es la que puede atrapar
    # una sesion en el -k2 sin key (ver ensure_alibaba_key2_active): sin
    # key no hay sid, no hay pin, y manda el filtro de `order`.
    if not alibaba_key2_disponible():
        return None
    ensure_affinity_redis()
    sid = _session_id(data) or _prefix_affinity_key(data)
    if not sid:
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        data["metadata"] = metadata
    metadata["session_id"] = str(sid)
    return metadata["session_id"]


# ── Afinidad por CUENTA de Alibaba (28-09-2026) ──────────────────────────────
#
# Una sesion vive en UNA cuenta, en TODOS los grupos `alibaba-*`, y no se mezcla.
# Sustituye al session_affinity nativo del Router (DeploymentAffinityCheck), que
# auditado el 28-09 contra la fuente pineada v1.100.0 tenia tres huecos:
#   1. El pin era POR GRUPO (`deployment_affinity:v1:session:<grupo>:...`): la
#      misma conversacion en `alibaba-q38-max` y en `alibaba-q38-flash` podia
#      quedar en cuentas distintas.
#   2. Con el deployment clavado en cooldown, el filtro devolvia TODOS los sanos:
#      esa peticion iba a la otra cuenta, el pin seguia en la primera y la
#      sesion volvia a ella al acabar el cooldown — ida y vuelta, fria las dos.
#   3. TTL de 1 h de inactividad SIN proporcion al cupo: al volver tras el hueco
#      se re-sorteba a ciegas (50/50) y, con el hueco 2, la conversacion saltaba
#      de cuenta pagando el prefijo en cada salto. Lo que hace caro un re-sorteo
#      es que el prefijo AUN este vivo; pasado el hueco ya esta frio en las dos
#      cuentas, asi que mudar sale gratis y repartir bien importa (DGX-592).
#
# Aqui:
#   - Clave `alibaba_account_pin:v1:<api-key-hash>:<sid>` -> "k1" | "k2" en la
#     Valkey del session router (misma URL/password). TTL de inactividad (el del
#     vinculo de plan a Alibaba) renovado por keepalive en cada acierto.
#   - Cuenta de una sesion NUEVA = hash(api-key-hash, sid) % cuentas: las dos
#     replicas eligen LA MISMA sin hablar entre si, asi que ni un corte de
#     Valkey ni una carrera entre pods mezclan una sesion nueva. El SET NX solo
#     guarda la eleccion (y las mudanzas).
#   - Si la cuenta de la sesion no tiene deployment SANO en el grupo pedido
#     (cooldown, retirada), la sesion se MUDA entera a la otra cuenta y el pin
#     se reescribe: no hay ida y vuelta. Es la unica forma de cambiar de cuenta,
#     queda en el log y en `ACCOUNT_AFFINITY_STATS["moves"]`.
#   - Sesiones que ya tenian pin nativo por grupo: se respeta esa cuenta la
#     primera vez (siembra), para que el cambio de mecanismo no mueva a nadie.
#   - Sin sid (ni cabecera ni prefijo) no se filtra: no hay sesion que clavar.
# CONTRACT: dgx.session-router.alibaba-account-pin.v1
# (ancla: alibaba_account_pin:v1:) valor "k1" | "k2", Valkey del session router.
ALIBABA_ACCOUNT_PIN_PREFIX = "alibaba_account_pin:v1"
# TTL DE INACTIVIDAD (se renueva en cada peticion), el MISMO tiempo que el
# vinculo de plan a Alibaba (STICKY_TTL_ALIBABA_SECONDS, DGX-592): lo
# unico que defiende el pin es la caché de prefijo de la cuenta, y esa caché no
# aguanta pausas largas (ver el comentario de STICKY_TTL_*). Los 30 dias de antes
# eran el ciclo del plan, no la vida del prefijo: una sesion nacida en k1 se
# quedaba en k1 aunque su prefijo llevara dias frio, asi que el reparto por cupo
# nunca reasignaba nada (medido: pesos 0,28/0,72 y 51/49 de trafico REAL a favor
# de la cuenta SIN cupo; 0 mudanzas en 4 h). Una sesion viva no muda —el keepalive
# renueva—; una que vuelve tras una hora se re-sortea con los pesos. El coste es
# cero: ese prefill frio ya lo pagaba en la cuenta vieja.
ALIBABA_ACCOUNT_PIN_TTL_SECONDS = int(os.environ.get(
    "ALIBABA_ACCOUNT_PIN_TTL_SECONDS", str(STICKY_TTL_ALIBABA_SECONDS)))
_NATIVE_PIN_PREFIX = "deployment_affinity:v1:session"
_ACCOUNT_RE = re.compile(r"-(k[0-9]+)$")
_ACCOUNT_KEY_RE = re.compile(r"k[0-9]+")
# Pines conocidos por ESTE proceso: si Valkey no contesta a tiempo, una sesion
# que ya se vio aqui sigue en su cuenta (incluida una mudanza reciente). Caduca
# por TIEMPO, al mismo ritmo que el pin de Valkey (DGX-592): sin caducidad local,
# un GET correcto que devuelve None —pin caducado— rebote en la memoria del pod y
# la sesion no se re-sorteo nunca, que es el fallo que este cambio venia a cerrar.
_account_pins_local = {}
_ACCOUNT_PINS_LOCAL_MAX = 20000


def _remember_pin(key, account):
    """Memoria local acotada (orden de insercion: se cae lo mas viejo) y con
    caducidad: dura lo que duraria el pin en Valkey desde este acierto."""
    _account_pins_local.pop(key, None)
    _account_pins_local[key] = (account, time.monotonic() + ALIBABA_ACCOUNT_PIN_TTL_SECONDS)
    while len(_account_pins_local) > _ACCOUNT_PINS_LOCAL_MAX:
        _account_pins_local.pop(next(iter(_account_pins_local)))


def _local_pin(key):
    """Cuenta recordada por este pod, solo si aun no caduco (ver _remember_pin)."""
    hit = _account_pins_local.get(key)
    if not hit:
        return None
    account, deadline = hit
    if deadline <= time.monotonic():
        _account_pins_local.pop(key, None)
        return None
    return account
ACCOUNT_AFFINITY_STATS = {"hits": 0, "claims": 0, "moves": 0, "seeded": 0, "weighted": 0, "redis_errors": 0}


def _deployment_account(deployment):
    """"k1" / "k2" del id estable del deployment (`<grupo>-kN`), o None."""
    info = deployment.get("model_info") if isinstance(deployment, dict) else None
    model_id = info.get("id") if isinstance(info, dict) else None
    m = _ACCOUNT_RE.search(str(model_id or ""))
    return m.group(1) if m else None


def _affinity_user_key(request_kwargs):
    """Mismo hash de key que el DeploymentAffinityCheck: sha256 salvo que ya lo sea."""
    for name in ("litellm_metadata", "metadata"):
        md = request_kwargs.get(name)
        if isinstance(md, dict) and md.get("user_api_key_hash") is not None:
            raw = str(md["user_api_key_hash"])
            if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
                return raw.lower()
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return "unscoped"


def _affinity_session_id(request_kwargs):
    for name in ("litellm_metadata", "metadata"):
        md = request_kwargs.get(name)
        if isinstance(md, dict) and md.get("session_id") is not None:
            return str(md["session_id"])
    return None


def account_pin_key(user_key, sid):
    return f"{ALIBABA_ACCOUNT_PIN_PREFIX}:{user_key}:{sid}"


def preferred_account(user_key, sid, accounts, weights=None):
    """Cuenta determinista para una sesion nueva: la misma en todos los pods.

    Con `weights` (29-09-2026, `alibaba_account_weights` de /api/model-routing/config:
    el cupo diario que le queda a cada cuenta) el hash se lee como un punto en [0, 1) y
    cae en la cuenta cuyo tramo acumulado lo contiene: una cuenta con el doble de cupo
    recibe el doble de sesiones nuevas. Sigue siendo determinista (mismos pesos = misma
    cuenta en los dos pods). Pesos que no cubren todas las cuentas o que suman 0 => el
    reparto uniforme de siempre."""
    ordered = sorted(accounts)
    digest = hashlib.sha256(f"{user_key}:{sid}".encode("utf-8")).digest()
    h = int.from_bytes(digest[:8], "big")
    # UNA sola formula para los dos casos (29-09-2026). Antes el reparto con
    # pesos leia el hash como un punto en [0, total) y el uniforme como
    # `h % len`: dos lecturas DISTINTAS del mismo hash, asi que una sesion
    # nueva caia en una cuenta u otra segun si ESE pod tenia o no la caché de
    # config cargada (pesos => tramos; {} en frio => módulo). Con el panel
    # proyectando pesos desde el 29-09, los pods transitaban {} -> pesos en
    # instantes distintos y el rojo de CI (run 36583219046: 46 sesiones
    # mezcladas) era eso, no el reparto. Sin pesos = pesos IGUALES, que es la
    # misma formula con tramos de 1: determinista entre pods con la caché fria
    # o caliente.
    if weights and all(a in weights for a in ordered):
        tramos = {a: float(weights[a]) for a in ordered}
    else:
        tramos = {a: 1.0 for a in ordered}
    total = sum(tramos[a] for a in ordered)
    if total <= 0:
        tramos = {a: 1.0 for a in ordered}
        total = float(len(ordered))
    point = h / 2 ** 64 * total
    acc = 0.0
    for a in ordered:
        acc += tramos[a]
        if point < acc:
            return a
    return next(a for a in reversed(ordered) if tramos[a] > 0)


async def _account_weights():
    """Pesos por cuenta de la config del panel (caché SWR, sin I/O en el camino). {} si no hay."""
    try:
        weights = (await _config()).get("alibaba_account_weights")
        return weights if isinstance(weights, dict) else {}
    except Exception:
        return {}


async def _pin_get(client, key):
    return await asyncio.wait_for(client.get(key), timeout=REDIS_OP_TIMEOUT_SECONDS)


async def _pin_claim(client, key, account):
    """SET NX: gana el primero. Devuelve la cuenta que queda guardada."""
    ok = await asyncio.wait_for(
        client.set(key, account, nx=True, ex=ALIBABA_ACCOUNT_PIN_TTL_SECONDS),
        timeout=REDIS_OP_TIMEOUT_SECONDS,
    )
    if ok:
        return account
    current = await _pin_get(client, key)
    return current or account


async def _pin_keepalive(client, key):
    try:
        await asyncio.wait_for(client.expire(key, ALIBABA_ACCOUNT_PIN_TTL_SECONDS),
                               timeout=STICKY_WRITE_TIMEOUT_SECONDS)
    except Exception:
        ACCOUNT_AFFINITY_STATS["redis_errors"] += 1


async def _native_pin_account(client, model_group, user_key, sid):
    """Cuenta del pin nativo por grupo (anterior al 28-09), para sembrar sin mover."""
    try:
        raw = await _pin_get(client, f"{_NATIVE_PIN_PREFIX}:{model_group}:{user_key}:{sid}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
        model_id = value.get("model_id") if isinstance(value, dict) else value
    except ValueError:
        model_id = raw
    m = _ACCOUNT_RE.search(str(model_id or ""))
    return m.group(1) if m else None


async def alibaba_account_filter(model, healthy_deployments, request_kwargs):
    """Deja en `healthy_deployments` solo los de la cuenta de la sesion.

    Lo llama el Router en CADA eleccion de deployment, reintentos incluidos, a
    traves de StripUnsupportedParams.async_filter_deployments (el callback que
    ya existe: este modulo no es un callback de litellm, mandato 2). Fail-open: cualquier
    cosa que no sea un grupo `alibaba-*` con ids `-kN` y un sid pasa intacta."""
    if not str(model or "").startswith(ALIBABA_PREFIX) or not healthy_deployments:
        return healthy_deployments
    by_account = {}
    for dep in healthy_deployments:
        account = _deployment_account(dep)
        if account is None:
            return healthy_deployments
        by_account.setdefault(account, []).append(dep)
    request_kwargs = request_kwargs or {}
    sid = _affinity_session_id(request_kwargs)
    if not sid:
        return healthy_deployments
    user_key = _affinity_user_key(request_kwargs)
    key = account_pin_key(user_key, sid)

    client = None
    pinned = None
    leido = False
    try:
        client = await _redis()
        if client is not None:
            pinned = await _pin_get(client, key)
            leido = True
    except Exception:
        ACCOUNT_AFFINITY_STATS["redis_errors"] += 1
        client = None
    if pinned is None and not leido:
        # Red de estabilidad SOLO cuando Valkey no contesto (caida, lenta, sin
        # tier montado). Un GET CORRECTO que devuelve None es un pin CADUCADO,
        # no una lectura fallida: reactivarlo desde la memoria del pod clavaba
        # la sesion en su cuenta de nacimiento para siempre (DGX-592).
        pinned = _local_pin(key)

    if pinned in by_account:
        ACCOUNT_AFFINITY_STATS["hits"] += 1
        _remember_pin(key, pinned)
        if client is not None:
            _schedule(_pin_keepalive(client, key))
        return by_account[pinned]

    if pinned is not None:
        # Su cuenta no tiene deployment sano en este grupo: mudanza ENTERA.
        target = preferred_account(user_key, sid, by_account, await _account_weights())
        ACCOUNT_AFFINITY_STATS["moves"] += 1
        log.warning("session_router: sesion %s muda de cuenta Alibaba %s -> %s (%s sin deployment sano)",
                    sid[:24], pinned, target, model)
        _remember_pin(key, target)
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.set(key, target, ex=ALIBABA_ACCOUNT_PIN_TTL_SECONDS),
                    timeout=REDIS_OP_TIMEOUT_SECONDS,
                )
            except Exception:
                ACCOUNT_AFFINITY_STATS["redis_errors"] += 1
        return by_account[target]

    # Sesion sin pin: la cuenta que ya tenia con el pin nativo, o la determinista.
    choice = None
    if client is not None:
        seeded = await _native_pin_account(client, model, user_key, sid)
        if seeded in by_account:
            choice = seeded
            ACCOUNT_AFFINITY_STATS["seeded"] += 1
    if choice is None:
        weights = await _account_weights()
        choice = preferred_account(user_key, sid, by_account, weights)
        if weights:
            ACCOUNT_AFFINITY_STATS["weighted"] += 1
    if client is not None:
        try:
            won = await _pin_claim(client, key, choice)
            if won in by_account:
                choice = won
        except Exception:
            ACCOUNT_AFFINITY_STATS["redis_errors"] += 1
    ACCOUNT_AFFINITY_STATS["claims"] += 1
    _remember_pin(key, choice)
    return by_account[choice]


async def alibaba_switches():
    """Los cuatro interruptores «Fallbacks a Alibaba» (DEFAULT_ALIBABA) tal como los
    ve este módulo: caché SWR de la config del panel, sin I/O en el camino de la
    petición. Fail-open: cualquier fallo = todos encendidos (= antes del 25-09)."""
    try:
        config = await _config()
        sw = config.get("alibaba") if isinstance(config.get("alibaba"), dict) else {}
        return {k: sw.get(k) is not False for k in DEFAULT_ALIBABA}
    except Exception as exc:
        _warn_throttled("alibaba_switches", f"alibaba_switches falló ({exc}); fail-open")
        return dict(DEFAULT_ALIBABA)


async def apply_company_policy(data, requested_model):
    """Interruptor «Fallback Alibaba» de la compañía. Lo llama litellm_strip_params en su
    async_pre_call_hook justo después de la política de fallbacks por key, ANTES de
    resolver alias y de apply_session_routing.

    Devuelve (denegada, detalle). Sin clase company o con Alibaba permitido: (False, None)
    sin tocar nada. Con Alibaba desactivado: un `alibaba-*` pedido o resuelto -> (True,
    detalle para un 403); cualquier otro -> sella `disable_fallbacks` y (False, None).
    Fail-open: cualquier fallo o config fría = (False, None) = comportamiento anterior."""
    try:
        if _claude_class(data) != COMPANY_CLASS:
            return False, None
        config = await _config()
        if (config.get("company") or {}).get("alibaba", True) is not False:
            return False, None
        for name in (requested_model, data.get("model")):
            if str(name or "").lower().startswith(ALIBABA_PREFIX):
                log.warning(
                    "session_router: company pidio %r con el fallback a Alibaba desactivado -> 403",
                    name,
                )
                return True, {
                    "error": "company_alibaba_disabled",
                    "model": str(name),
                    "hint": ("La compañía tiene Alibaba desactivado en dgx.e-dani.com/"
                             "claude-sessions#settings: usa el LLM local."),
                }
        data["disable_fallbacks"] = True
        return False, None
    except Exception as exc:
        _warn_throttled("company_policy", f"apply_company_policy falló ({exc}); fail-open")
        return False, None


async def apply_session_routing(data, requested_model, tracker=None, resident_ready=True):
    """Punto de entrada único (lo llama litellm_strip_params.async_pre_call_hook
    tras la red final de visión y antes del sampling por familia).

    Devuelve True SI Y SOLO SI reescribió la petición residente -> alibaba; el
    llamador usa ese booleano como marcador `_session_routing_rerouted` para que
    la admisión compute-mode no 503-e el desvío (mandato 3).

    `resident_ready` lo aporta el llamador desde su propia cache de compute-mode
    (_compute_mode_state + _compute_mode_allows_local): mandato 9, no montar
    aquí una tercera cache del árbitro.
    """
    info = {
        "model": str(data.get("model") or ""), "sid": None, "bound": None,
        "fresh": None, "plan": None, "cool": None, "inflight": None,
        "class": None, "exempt": None, "est": None, "kv": None,
        "decision": "fail-open", "active": False,
    }
    try:
        rewrote = await _apply(data, requested_model, tracker, bool(resident_ready), info)
    except Exception as exc:
        log.warning("session_router: fallo inesperado (%s); fail-open", exc)
        return False
    # Una línea por petición cuando el routing de sesión está en juego
    # (defecto 2 del QA live: default_plan=alibaba no reescribía y NO dejaba
    # rastro — la causa real era invisible). Sin features activos, silencio:
    # el tráfico normal no gana ruido de log. IMPORTANTE: en el proceso proxy
    # el logger solo deja pasar WARNING+ (medido en vivo 21-09: las líneas
    # INFO de _rewrite y de decisión no aparecían jamás) — toda decisión
    # NOTABLE (reescritura o degradación) va por WARNING; el resto INFO.
    if info["active"] or rewrote:
        notable = rewrote or "degradado" in str(info["decision"])
        emit = log.warning if notable else log.info
        emit(
            "session_router decision: model=%s sid=%s class=%s exempt=%s bound=%s plan=%s "
            "cool=%s inflight=%s est=%s kv=%s -> %s%s",
            info["model"] or "-", info["sid"] or "-", info["class"] or "-", info["exempt"],
            info["bound"], info["plan"],
            info["cool"], info["inflight"], info["est"], info["kv"], info["decision"],
            " (REESCRITA)" if rewrote else "",
        )
    return rewrote


def _overflow_ok_info(info):
    """_overflow_ok() dejando rastro del estado de cooldown en la decisión."""
    cool = _group_in_cooldown(OVERFLOW_MODEL)
    info["cool"] = cool
    return cool is False


async def _apply(data, requested_model, tracker, resident_ready, info):
    model = str(data.get("model") or "")
    if model not in ROUTED_MODELS:
        info["decision"] = "no-routed"
        return False
    # Mandato 4a: petición sellada (disable_fallbacks, estampado aguas arriba
    # por strip_params) nunca abandona el backend que pidió.
    if data.get("disable_fallbacks") is True:
        info["decision"] = "sellada"
        return False
    # Mandato 4b: uncensored nunca cae a Alibaba. Por NOMBRE (pedido y
    # resuelto): el sello se estampa después de este punto.
    if _is_uncensored(requested_model) or _is_uncensored(model):
        info["decision"] = "uncensored"
        return False

    config = await _config()
    sid = _session_id(data) or _prefix_affinity_key(data)
    info["sid"] = sid
    # La clase se lee aquí (no dentro de la válvula) para que la línea de
    # decisión la lleve también en los caminos que la rodean (sellado, plan
    # explícito...): la precedencia NO la mira, solo la visibilidad.
    claude_class = _claude_class(data)
    info["class"] = claude_class
    company = claude_class == COMPANY_CLASS
    # Interruptor «Fallback Alibaba» de la compañía (23-09-2026): activado, la compañía
    # desborda a Alibaba al llenarse el residente (su propia válvula, abajo); desactivado,
    # ni llega aquí — apply_company_policy la sella antes y la precedencia 1 la deja pasar.
    company_overflow = company and (config.get("company") or {}).get("alibaba", True) is not False
    key_alias = _key_alias(data)
    exempt = _overflow_exempt(config, claude_class, key_alias)
    info["exempt"] = exempt
    info["active"] = bool(
        config["sticky"] or config["instant_reject"]
        or config["default_plan"] != "local" or config["session_plans"]
    )
    # Presupuesto KV (27-09-2026, precedencia 4b/4c): los sondeos que lo
    # alimentan (capacidad en /metrics, suma de sesiones en el HASH) se
    # aseguran aquí; fuera del camino de la petición, duermen si el routing de
    # sesión no está en juego. La estimación del prompt se hace UNA vez por
    # petición residente y solo con sticky activo (sin binding no hay válvula
    # que la use ni retorno que decidir).
    if config["sticky"]:
        _ensure_vllm_poller()
        _ensure_sessions_poller()
    est = None
    if config["sticky"] and sid and model == RESIDENT_MODEL:
        est = _estimate_prompt_tokens(data, tracker)
        info["est"] = est
    is_bot = _is_bot_session(config, claude_class, key_alias)
    # Interruptor global «Desborde del session-router» (25-09-2026): apagado, ninguna
    # reescritura a Alibaba de este módulo — ni plan explícito, ni sticky, ni válvula
    # (tampoco la de la compañía). La petición se queda en el residente y encola.
    if (config.get("alibaba") or {}).get("overflow", True) is False:
        info["decision"] = "desborde_alibaba_apagado"
        return False

    # ── Precedencia 3: plan EXPLÍCITO del panel (decisión del operador) ──
    # Vive ANTES del corte por flags: un plan explícito es una orden directa
    # del operador y aplica aunque sticky/instant_reject estén apagados. Lo que
    # los flags gobiernan son los mecanismos AUTOMÁTICOS (vinculación sticky y
    # válvula de capacidad), no la voluntad explícita del panel.
    explicit = None
    if sid:
        plan_value = config["session_plans"].get(sid)
        if plan_value in PLANES:
            explicit = plan_value
    if explicit == "local":
        # Al residente y ENCOLA si está lleno: nunca rechazo instantáneo.
        info["plan"] = "local(explicito)"
        info["decision"] = "plan_explicito_local"
        if est is not None:
            # Ocupa cache igual que cualquier sesión de plan default: cuenta
            # en la suma del presupuesto (4b). Su binding NO se escribe: el
            # sticky es solo para sesiones de plan default (mandato 6).
            _sessions_write(sid, est)
        return False
    if explicit == "claude":
        # No aplica en este hook: Anthropic ni pasa por LiteLLM. La puerta vive
        # en el claude-router del x86 (PR-C4).
        info["plan"] = "claude(explicito)"
        info["decision"] = "plan_explicito_claude_puerta_en_router"
        return False
    if explicit == "alibaba":
        info["plan"] = "alibaba(explicito)"
        if model == RESIDENT_MODEL and _overflow_ok_info(info):
            info["decision"] = "plan_alibaba"
            _rewrite(data, sid, "plan_alibaba")
            return True
        info["decision"] = (
            "plan_explicito_alibaba_degradado" if info["cool"] is not False
            else "plan_explicito_alibaba_modelo_no_residente")
        return False

    if not config["sticky"] and not config["instant_reject"] and not company_overflow:
        info["decision"] = "flags_apagados"
        return False

    # ── Precedencia 4/5: sesiones de plan DEFAULT (sticky > válvula) ──
    bound = None
    if config["sticky"] and sid:
        bound = await _sticky_get(sid)
    fresh = bound is None
    if fresh:
        plan = config["default_plan"] if config["sticky"] else "local"
    else:
        plan = bound
    # Exento (26-09-2026): un binding a Alibaba que NO viene de una orden del
    # operador (default_plan=alibaba) se suelta al residente si está Ready. Sin
    # esto la exención no liberaría a las sesiones ya capturadas por la válvula.
    if (exempt and not fresh and plan == "alibaba" and resident_ready
            and config["default_plan"] != "alibaba"):
        # (el binding a local lo escribe el final del camino local, abajo)
        log.warning("session_router: sesion exenta sid=%s class=%s soltada de alibaba -> residente",
                    sid, claude_class or "-")
        plan = "local"
        bound = "local(soltada)"
    info["bound"] = bound
    info["fresh"] = fresh
    info["plan"] = plan
    if plan == "claude":
        info["decision"] = "plan_claude_puerta_en_router"
        return False

    if plan == "alibaba":
        if not _overflow_ok_info(info):
            # Alibaba en cooldown o desconocido: degradar. Si el binding era
            # sticky y el residente admite, se re-vincula (hueco).
            if config["sticky"] and sid and not fresh and resident_ready:
                await _sticky_set(sid, "local")
                _albind_clear(sid)
                if est is not None:
                    _sessions_write(sid, est)
            info["decision"] = "plan_alibaba_degradado_por_cooldown"
            return False
        # Regla de retorno (4c, 27-09-2026): una sesión ligada a Alibaba NO
        # vuelve por vencimiento de TTL (subió de 300 s a 3600 s para que una
        # pausa corta no la traiga fría a mitad de conversación); vuelve solo
        # si compactó (prompt <= return_max_tokens) y cabe, o si nació en
        # Alibaba hace >= alibaba_return_after_s y cabe. default_plan=alibaba
        # es orden del operador y no se deshace (criterio igual que exentos);
        # un plan EXPLÍCITO alibaba ni llega aquí (precedencia 3).
        if (not fresh and config["sticky"] and sid and resident_ready
                and model == RESIDENT_MODEL and config["default_plan"] != "alibaba"):
            motivo = await _may_return_local(config, sid, est, is_bot)
            if motivo:
                await _sticky_set(sid, "local")
                _albind_clear(sid)
                if est is not None:
                    _sessions_write(sid, est)
                info["decision"] = "retorno_local_" + motivo
                log.warning(
                    "session_router: retorno a local sid=%s motivo=%s est=%s bot=%s",
                    sid, motivo, est, is_bot,
                )
                return False
        if config["sticky"] and sid:
            # fresh: vincula; ligada: renueva el TTL (afinidad = caché de Alibaba)
            await _sticky_set(sid, "alibaba")
            _albind_touch(sid)
        if model == RESIDENT_MODEL:
            info["decision"] = "sticky_alibaba" if not fresh else "default_alibaba"
            _rewrite(data, sid, "sticky_alibaba" if not fresh else "default_alibaba")
            return True
        info["decision"] = "plan_alibaba_modelo_no_residente"
        return False

    # plan == "local"
    if not resident_ready:
        # Hueco: compute-mode no admite el residente ahora mismo. Re-vincula a
        # Alibaba solo si está utilizable; si no, degrada (la admisión de
        # strip_params decidirá con su propio criterio).
        if _overflow_ok_info(info):
            if config["sticky"] and sid:
                await _sticky_set(sid, "alibaba")
                _albind_touch(sid)
            if model == RESIDENT_MODEL:
                info["decision"] = "rebind_alibaba"
                _rewrite(data, sid, "rebind_alibaba")
                return True
        info["decision"] = "residente_no_ready_degradado"
        return False

    # Válvula de capacidad (precedencia 5) — solo plan default. Con sticky
    # activo, el disparo RE-VINCULA la sesión (destino sano = hueco libre);
    # con sticky apagado es una válvula por petición que no escribe nada.
    # Quién pasa por la válvula (23-09-2026, deroga el "la compañía nunca salta"
    # del 21-09/22-09): las sesiones de la compañía (x-claude-class: company) la
    # gobierna SU interruptor «Fallback Alibaba» del panel Settings, no
    # instant_reject — activado, desbordan a Alibaba en cuanto el residente está
    # lleno (medido el 23-09 con MAX_NUM_SEQS=16: cola vLLM p50 ~19 s, p95 ~87 s,
    # TTFT p95 ~142 s, y la compañía sin salida); desactivado, ya vienen selladas.
    # El resto de sesiones, como siempre: solo con instant_reject. Sellado,
    # uncensored, plan explícito, sticky ya ligado y re-bind aplican igual.
    #
    # 23-09-2026 (Dani, "sin esperas"): la válvula salta por dos motivos —
    #   lleno: el presupuesto ÚNICO del residente (todos sus nombres) en local_slots;
    #   cola:  la cola de vLLM lleva WAIT_TOLERANCE_SECONDS sin vaciarse —
    # y MUDA LA SESIÓN a Alibaba (sticky, renovado en cada petición). Desviar solo la
    # petición (lo que hizo #134 unas horas) fue un error medido: la sesión saltaba
    # petición a petición entre el local y Alibaba y llegaba FRÍA a los dos (Alibaba
    # 22 % de acierto de caché, 198 M tokens en 4 h). Con la sesión quieta la caché
    # funciona en los dos lados — medido el mismo día: Alibaba 19.712/20.553 y
    # 20.480/20.803 tokens cacheados en turnos seguidos; vLLM 22.400/28.046.
    valvula = not exempt and (
        (company and company_overflow) or (not company and config["instant_reject"]))
    if valvula and model == RESIDENT_MODEL:
        _ensure_vllm_poller()
        inflight = await _inflight_resident(tracker)
        info["inflight"] = inflight
        stuck = _queue_stuck()
        lleno = inflight is not None and inflight >= config["local_slots"]
        if (lleno or stuck) and _overflow_ok_info(info):
            if config["sticky"] and sid:
                await _sticky_set(sid, "alibaba")
                _albind_touch(sid)
            razon = "company_overflow" if company else "instant_reject"
            motivo = "lleno" if lleno else "cola"
            info["decision"] = f"{razon}_{motivo}"
            _rewrite(data, sid, razon)
            data["metadata"]["_session_routing_trigger"] = motivo
            log.warning(
                "session_router: %s (%s) sid=%s en_vuelo=%s slots=%s cola_vllm=%s -> %s",
                razon, motivo, sid or "-", inflight, config["local_slots"],
                _vllm_queue["waiting"], OVERFLOW_MODEL,
            )
            return True

    # ── Válvula de presupuesto KV (4b, 27-09-2026) ──
    # SOLO sesiones NUEVAS (fresh): una ya ligada a local sigue en local —
    # expulsar un prefijo caliente paga el re-prefrío de quien se va y el de
    # quien vuelve, que es exactamente el bucle de thrash que esta válvula
    # evita. Exentas: encolan (overflow_exempt = no desborda por CAPACIDAD, y
    # esto es capacidad). Sin capacidad descubierta, sin HASH fresco o sin
    # estimación: apagada (fail-open = comportamiento actual). Va DESPUÉS de
    # la válvula en-vuelo/cola: si esa ya mudó la sesión, esta no decide.
    if fresh and config["sticky"] and sid and not exempt and model == RESIDENT_MODEL:
        state = _kv_budget_state(config, est, is_bot)
        if state is not None:
            limit, capacity, total = state
            info["kv"] = f"{int(total)}/{int(limit)}"
            if est + total > limit and _overflow_ok_info(info):
                await _sticky_set(sid, "alibaba")
                _albind_touch(sid)
                info["decision"] = "kv_budget_alibaba"
                _rewrite(data, sid, "kv_budget")
                data["metadata"]["_session_routing_trigger"] = "presupuesto"
                log.warning(
                    "session_router: presupuesto KV (%s) sid=%s est=%s suma_vivas=%s/%s cap=%s n=%s -> %s",
                    "bot" if is_bot else "default", sid, est, int(total), int(limit),
                    capacity, _kv_sessions["n"], OVERFLOW_MODEL,
                )
                return True

    if config["sticky"] and sid:
        # fresh: vincula; ligada: renueva el TTL (afinidad = caché del local)
        await _sticky_set(sid, "local")
        if not fresh and bound != "local":
            # Volvió de Alibaba (soltada por exención o re-bind): el reloj de
            # nacimiento (4c) empieza de cero en la próxima mudanza.
            _albind_clear(sid)
    if est is not None:
        # Anotación de consumo: esta petición ocupará (o ya ocupa) su prefijo
        # en la KV cache del residente; es la suma que lee 4b/4c.
        _sessions_write(sid, est)
    info["decision"] = "local"
    return False


# DGX-453 (29-09-2026): prioridad de cola del RESIDENTE segun el alias de la
# virtual key. El mapa vive en /config/config.yaml (`priority_by_alias`,
# top-level) y NO en este modulo: se reordena la cola sin tocar codigo.
# Fail-open TOTAL: cualquier fallo (fichero ausente, yaml roto, forma rara)
# => no se inyecta nada y la peticion sale con la prioridad default de vLLM.
# Solo hacia `*.llm.svc.cluster.local`: Alibaba y OpenRouter no conocen el
# campo y devolverian 400.
# CONTRACTS.yaml: dgx.litellm.priority-field.v1
_PRIORITY_CONFIG_PATH = "/config/config.yaml"
_PRIORITY_RESIDENT_MARKER = ".llm.svc.cluster.local"
_priority_cache = {"mtime": None, "map": None, "model_list": None}


def _priority_config():
    """(mapa alias->int, model_list) del config, reparseado SOLO si el mtime
    del fichero cambio (esto corre en cada peticion). Devuelve (None, None)
    ante cualquier fallo."""
    try:
        mtime = os.path.getmtime(_PRIORITY_CONFIG_PATH)
    except OSError:
        return None, None
    if _priority_cache["mtime"] != mtime:
        try:
            import yaml

            with open(_PRIORITY_CONFIG_PATH) as fh:
                cfg = yaml.safe_load(fh) or {}
            raw = cfg.get("priority_by_alias")
            pmap = None
            if isinstance(raw, dict):
                pmap = {}
                for k, v in raw.items():
                    if (isinstance(k, str) and isinstance(v, int)
                            and not isinstance(v, bool)):
                        pmap[k.strip().lower()] = v
            ml = cfg.get("model_list")
            _priority_cache["map"] = pmap
            _priority_cache["model_list"] = ml if isinstance(ml, list) else None
            _priority_cache["mtime"] = mtime
        except Exception:
            return None, None
    return _priority_cache["map"], _priority_cache["model_list"]


def _priority_targets_resident(model, model_list):
    """El modelo RESUELTO (data["model"], ya elegido por la red final) apunta
    a un deployment cuyo api_base es del residente?"""
    if not isinstance(model, str) or not isinstance(model_list, list):
        return False
    for entry in list(model_list):
        try:
            names = str(entry["model_name"]).split(",")
            api_base = entry["litellm_params"]["api_base"]
        except (KeyError, TypeError):
            continue
        if (any(n.strip() == model for n in names)
                and _PRIORITY_RESIDENT_MARKER in str(api_base)):
            return True
    return False


def apply_priority(data):
    """Inyecta `priority` (int) en data["extra_body"] segun el alias de la
    virtual key, SOLO si el destino resuelto es el residente. Sin alias, alias
    no mappeado, prioridad 0 o destino cloud => no se añade el campo. Nunca
    lanza. Llamado desde litellm_strip_params.async_pre_call_hook justo
    despues del pin de afinidad, donde data["model"] ya es el destino real."""
    try:
        alias = _key_alias(data)
        if not alias:
            return
        pmap, model_list = _priority_config()
        if not pmap:
            return
        prio = pmap.get(alias)
        if not isinstance(prio, int) or isinstance(prio, bool) or prio == 0:
            return
        if not _priority_targets_resident(data.get("model"), model_list):
            return
        body = data.get("extra_body")
        if not isinstance(body, dict):
            body = {}
            data["extra_body"] = body
        body.setdefault("priority", prio)
    except Exception:
        return
