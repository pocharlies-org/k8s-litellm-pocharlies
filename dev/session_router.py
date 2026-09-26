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
#   - Valkey dedicado (litellm-valkey): SOLO el mapa sticky sid -> "local" |
#     "alibaba" con TTL 1 h. Prohibido INCR/DECR/ZSET: no hay contador aquí.
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
#   5. default con instant_reject: al llegar a local_slots en vuelo, reescribe
#      al instante a alibaba-q38-flash. SOLO sesiones de plan default.
#   El sticky en Valkey se escribe ÚNICAMENTE para sesiones de plan default.
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
import os
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
#   alibaba  5 min: tras una pausa la sesión vuelve a decidirse y regresa al
#           local si hay hueco. La caché implícita de Alibaba no aguanta pausas
#           largas, así que tras la pausa llegaría fría igual. Sin pausa se
#           renueva y no se mueve: nunca se trae al local a mitad de ráfaga.
STICKY_TTL_LOCAL_SECONDS = int(os.environ.get("SESSION_ROUTER_STICKY_TTL_LOCAL_S", "1800"))
STICKY_TTL_ALIBABA_SECONDS = int(os.environ.get("SESSION_ROUTER_STICKY_TTL_ALIBABA_S", "300"))
# CONTRACT: dgx.session-router.sticky-key.v1
STICKY_KEY_PREFIX = "session-router:sticky:"
DEFAULT_LOCAL_SLOTS = 8
LOCAL_SLOTS_CAP = 64
PLANES = ("local", "alibaba", "claude")

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
    slots = raw.get("local_slots")
    if isinstance(slots, int) and not isinstance(slots, bool) and 0 <= slots <= LOCAL_SLOTS_CAP:
        config["local_slots"] = slots
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
        except Exception as exc:
            _warn_throttled("vllm_poll", f"sondeo de la cola de vLLM falló ({exc.__class__.__name__}); sin válvula de cola")
        await asyncio.sleep(VLLM_POLL_SECONDS)


def _ensure_vllm_poller():
    """Arranca el sondeo la primera vez que hace falta (y lo rearranca si murió)."""
    global _vllm_poller
    if _vllm_poller is None or _vllm_poller.done():
        _vllm_poller = asyncio.create_task(_poll_vllm_forever())


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


# ── Afinidad de sesion por CUENTA de Alibaba (26-09-2026) ────────────────
# Cada grupo `alibaba-*` tiene dos deployments (dos planes Team). El cache
# de contexto de Model Studio es POR CUENTA: partir una conversacion entre
# las dos paga el prefijo entero en cada salto. El mecanismo es el nativo
# del Router (DeploymentAffinityCheck, activado por grupo en
# router_settings.model_group_affinity_config, verificado en la fuente
# pineada v1.100.0); este modulo solo lo alimenta:
#   (a) estampa metadata["session_id"] con el sid PROPIO del hook en toda
#       peticion que va a un grupo `alibaba-*`. El id que manda el cliente
#       no vale — se PISA deliberadamente (AFFINITY_CHARS: opencode manda
#       22 distintos por conversacion).
#   (b) monta una vez la Valkey del namespace sobre el DualCache del
#       Router, sin la cual el pin vive solo en el pod y con dos replicas
#       la afinidad se rompe entre pods.
# Fail-open en los dos: sin sid no se estampa nada (shuffle puro, como
# hoy); sin Valkey el pin queda por replica y el router sigue sirviendo.

# CONTRACT: dgx.session-router.deployment-affinity-key.v1
# (ancla: deployment_affinity:v1:) Las claves de afinidad en Valkey db 0
# las escribe el DeploymentAffinityCheck de litellm SOBRE EL TIER que
# monta ensure_affinity_redis: cambiar la URL/db o quitar el tier las
# mata todas (afinidad perdida en silencio, no error).

_affinity_redis_tried = False


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
    Devuelve el sid estampado o None. Sin sid no se toca nada."""
    if not str(data.get("model") or "").startswith(ALIBABA_PREFIX):
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
        "class": None,
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
            "session_router decision: model=%s sid=%s class=%s bound=%s plan=%s "
            "cool=%s inflight=%s -> %s%s",
            info["model"] or "-", info["sid"] or "-", info["class"] or "-",
            info["bound"], info["plan"],
            info["cool"], info["inflight"], info["decision"],
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
    info["active"] = bool(
        config["sticky"] or config["instant_reject"]
        or config["default_plan"] != "local" or config["session_plans"]
    )
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
            info["decision"] = "plan_alibaba_degradado_por_cooldown"
            return False
        if config["sticky"] and sid:
            # fresh: vincula; ligada: renueva el TTL (afinidad = caché de Alibaba)
            await _sticky_set(sid, "alibaba")
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
    valvula = (company and company_overflow) or (not company and config["instant_reject"])
    if valvula and model == RESIDENT_MODEL:
        _ensure_vllm_poller()
        inflight = await _inflight_resident(tracker)
        info["inflight"] = inflight
        stuck = _queue_stuck()
        lleno = inflight is not None and inflight >= config["local_slots"]
        if (lleno or stuck) and _overflow_ok_info(info):
            if config["sticky"] and sid:
                await _sticky_set(sid, "alibaba")
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

    if config["sticky"] and sid:
        # fresh: vincula; ligada: renueva el TTL (afinidad = caché del local)
        await _sticky_set(sid, "local")
    info["decision"] = "local"
    return False
