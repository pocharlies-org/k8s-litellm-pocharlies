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
SIDECAR_TIMEOUT_SECONDS = 0.1
STICKY_TTL_SECONDS = 3600
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


async def _sticky_set_bg(sid, plan):
    try:
        client = await _redis()
        if client is None:
            return
        await asyncio.wait_for(
            client.set(f"{STICKY_KEY_PREFIX}{sid}", plan, ex=STICKY_TTL_SECONDS),
            timeout=STICKY_WRITE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _warn_throttled("sticky_set", f"sticky_set falló ({exc.__class__.__name__}); fail-open")


async def _inflight_resident(tracker):
    """Peticiones en vuelo al residente (mandato 1): este pod por el tracker
    in-process (exacto) + las OTRAS réplicas por el sidecar :4001, deduplicando
    por request_id (el agregado del sidecar incluye las filas de su propio
    fichero, que son este pod con hasta 0,25 s de retardo). None => contador
    desconocido => la válvula NO actúa (fail-open hacia admitir local, con el
    semaphore duro de 8 del Router detrás protegiendo igual)."""
    local_ids = set()
    local_n = 0
    try:
        snapshot = tracker.snapshot() if tracker is not None else {}
        for rid, row in (snapshot or {}).items():
            if isinstance(row, dict) and row.get("model") == RESIDENT_MODEL:
                local_n += 1
                local_ids.add(rid)
    except Exception as exc:
        log.warning("session_router: tracker snapshot falló (%s); sin válvula", exc)
        return None
    remote_n = 0
    try:
        headers = {}
        master_key = os.environ.get("LITELLM_MASTER_KEY") or ""
        if master_key:
            headers["Authorization"] = f"Bearer {master_key}"
        async with httpx.AsyncClient(timeout=SIDECAR_TIMEOUT_SECONDS) as client:
            response = await client.get(SIDECAR_URL, headers=headers)
        response.raise_for_status()
        rows = (response.json() or {}).get("active") or []
        for row in rows:
            if (
                isinstance(row, dict)
                and row.get("model") == RESIDENT_MODEL
                and row.get("request_id") not in local_ids
            ):
                remote_n += 1
    except Exception:
        # Sidecar mudo/par caído: solo lo local, que subestima => admite de más
        # en local, la dirección fail-open. El semaphore 8 por pod sigue topando.
        remote_n = 0
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
    sid = _session_id(data)
    info["sid"] = sid
    # La clase se lee aquí (no dentro de la válvula) para que la línea de
    # decisión la lleve también en los caminos que la rodean (sellado, plan
    # explícito...): la precedencia NO la mira, solo la visibilidad.
    claude_class = _claude_class(data)
    info["class"] = claude_class
    company = claude_class == COMPANY_CLASS
    info["active"] = bool(
        config["sticky"] or config["instant_reject"]
        or config["default_plan"] != "local" or config["session_plans"]
    )

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

    if not config["sticky"] and not config["instant_reject"]:
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
        if config["sticky"] and sid and fresh:
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
    # INFRA-208 (22-09): las sesiones de la compañía (x-claude-class:
    # company) NUNCA saltan por la válvula — deciden `local` y la admisión
    # de strip_params ENCOLA (decisión de Dani 21-09). El gate es SOLO esta
    # válvula: sellado, uncensored, plan explícito, sticky ya ligado a
    # alibaba y re-bind por residente no-ready aplican igual — la cabecera
    # no esquiva precedencia ni salta cola.
    if config["instant_reject"] and model == RESIDENT_MODEL and not company:
        inflight = await _inflight_resident(tracker)
        info["inflight"] = inflight
        if inflight is not None and inflight >= config["local_slots"] and _overflow_ok_info(info):
            if config["sticky"] and sid:
                await _sticky_set(sid, "alibaba")
            info["decision"] = "instant_reject"
            _rewrite(data, sid, "instant_reject")
            log.warning(
                "session_router: instant_reject sid=%s en_vuelo=%s slots=%s -> %s",
                sid or "-", inflight, config["local_slots"], OVERFLOW_MODEL,
            )
            return True

    if config["sticky"] and sid and fresh:
        await _sticky_set(sid, "local")
    info["decision"] = "local"
    return False
