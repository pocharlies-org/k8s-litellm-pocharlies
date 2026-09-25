"""Contract de INFRA-208: sticky routing por sesión + rechazo instantáneo.

Codifica los 10 mandatos del veredicto del arquitecto (nota-tech-lead-diseno-
sticky-routing.md en la épica):

  1. Valkey SOLO para el mapa sticky: prohibido INCR/DECR/ZSET; el contador en
     vuelo sale del ActiveRequestTracker + el sidecar (:4001), y el sidecar
     publica counts_by_model como clave aditiva.
  2. session_router.py NO es un segundo callback: lo importa
     litellm_strip_params.py y lo llama desde su async_pre_call_hook, tras la
     red final de visión y antes de _apply_family_sampling.
  3. Todo rewrite residente->alibaba marca _session_routing_rerouted y la
     admisión compute-mode lo respeta (not session_rerouted).
  4. Guardas antes de reescribir: disable_fallbacks y nombres uncensored.
  5. Cooldown por API pública del Router; fallo de la consulta => degradar.
  6. Precedencia: sellado > admisión > plan explícito > sticky > default con
     instant_reject; el plan explícito NUNCA se rechaza; sticky solo se escribe
     para sesiones de plan default.
  7. El comentario de router_settings sobre la afinidad queda actualizado.
  9. La config del panel se cachea con TTL + refresco en background (SWR) + último-bueno.
 10. Este fichero: AST + forma del manifiesto + comportamiento.

Fail-open: timeouts del camino de petición <= 100 ms y redis.asyncio.
"""
import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

RESIDENT = "qwen38-flash-next"
OVERFLOW = "alibaba-q38-flash"
SID = "33283bc9-8ac3-4445-990c-c1c86d8add8c"

REDIS_COUNTER_ATTRS = {
    "incr", "decr", "incrby", "incrbyfloat", "decrby",
    "zadd", "zrem", "zcard", "zcount", "zrange", "zrangebyscore",
    "zremrangebyscore", "zincrby", "zscore",
}


@pytest.fixture(scope="module")
def docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


@pytest.fixture(scope="module")
def configmap(docs):
    return next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def router_src(configmap):
    return configmap["data"]["session_router.py"]


@pytest.fixture(scope="module")
def strip_src(configmap):
    return configmap["data"]["litellm_strip_params.py"]


@pytest.fixture(scope="module")
def sidecar_src(configmap):
    return configmap["data"]["active_requests_api.py"]


@pytest.fixture(scope="module")
def router_mod(router_src):
    """Ejecuta el módulo con httpx stubbeado (el entorno de tests no necesita
    la dependencia real; las pruebas de comportamiento stubbean las E/S)."""
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        module = types.ModuleType("session_router_under_test")
        exec(compile(router_src, "session_router.py", "exec"), module.__dict__)
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)
    return module


# ── Mandato 1: Valkey solo mapa sticky; contador = tracker + sidecar ─────────


def test_session_router_no_tiene_contador_redis(router_src):
    tree = ast.parse(router_src)
    usados = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.lower() in REDIS_COUNTER_ATTRS
    }
    assert not usados, f"contador Redis prohibido en el hook: {usados}"
    upper = router_src.upper()
    for palabra in ("ZADD", "ZREM", "INCRBY", "DECRBY", "ZSET"):
        # Los comentarios pueden citar la prohibición; el CÓDIGO no puede usarla.
        lineas_codigo = [
            l for l in router_src.splitlines()
            if palabra in l.upper() and not l.lstrip().startswith("#")
        ]
        assert not lineas_codigo, f"{palabra} en código: {lineas_codigo}"
    assert "INCR" not in upper.replace("INCR/DECR", "")


def test_session_router_usa_tracker_y_sidecar(router_src):
    assert "tracker.snapshot()" in router_src
    assert "request_id" in router_src  # dedupe local vs sidecar
    assert "/internal/active-requests" in router_src


def test_sidecar_publica_counts_by_model(sidecar_src):
    assert "def _counts_by_model" in sidecar_src
    # agregado, endpoint /local y camino degradado
    assert sidecar_src.count('"counts_by_model": _counts_by_model(') == 3


# ── Mandato 2: módulo importado por strip_params, NO callback ────────────────


def test_no_registrado_como_callback(configmap):
    settings = yaml.safe_load(configmap["data"]["config.yaml"])
    callbacks = settings["litellm_settings"]["callbacks"]
    assert not any("session_router" in str(c) for c in callbacks)
    assert "litellm_strip_params.proxy_handler_instance" in callbacks


def test_session_router_no_es_custom_logger(router_src):
    assert "CustomLogger" not in router_src
    assert "proxy_handler_instance" not in router_src


def test_strip_params_importa_session_router_tolerando_falta(strip_src):
    idx = strip_src.index("import session_router")
    window = strip_src[max(0, idx - 200):idx + 300]
    assert "try:" in window and "session_router = None" in window


def test_llamada_entre_vision_final_y_family_sampling(strip_src):
    # call sites (no los def): la llamada de visión lleva data.get, la de
    # sampling va indentada a 12 espacios dentro del hook.
    i_vision = strip_src.index('if _omit_images_for_blind_backends(data, data.get("model", "")):')
    i_call = strip_src.index("session_router.apply_session_routing")
    i_family = strip_src.index("\n        _apply_family_sampling(data, thinking_alias)")
    assert i_vision < i_call < i_family


# ── Mandato 3: marcador de reroute respetado por la admisión ─────────────────


def test_rewrite_marca_session_routing_rerouted(router_src):
    assert '_session_routing_rerouted' in router_src


def test_admision_respeta_el_marcador(strip_src):
    assert (
        "if not vision_diverted and not session_rerouted "
        "and _is_local_vllm_request(model, proxy_model, api_base):" in strip_src
    )
    # la única llamada a la admisión queda bajo ese guard
    assert strip_src.count("await _enforce_compute_mode_admission(") == 1


# ── Mandato 4: guardas disable_fallbacks y uncensored ────────────────────────


def test_guardas_antes_de_reescribir(router_src):
    tree = ast.parse(router_src)
    apply_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "_apply"
    )
    fuente = ast.get_source_segment(router_src, apply_fn)
    i_sellado = fuente.index('data.get("disable_fallbacks") is True')
    i_uncensored = fuente.index("_is_uncensored(requested_model)")
    i_rewrite = fuente.index("_rewrite(")
    assert i_sellado < i_rewrite and i_uncensored < i_rewrite
    assert '"-uncensored"' in router_src or "UNCENSORED_SUFFIX" in router_src


# ── Mandato 5: cooldown por API pública, nunca claves de cache ───────────────


def test_cooldown_por_api_publica(router_src):
    assert "get_model_ids" in router_src
    assert "get_active_cooldowns" in router_src
    assert "cooldown_cache" in router_src
    # nunca construye/parsea claves internas del cooldown
    assert "cooldown_deployment_id" not in router_src
    assert "get_keys" not in router_src


# ── Mandatos 9/10: fail-open, timeouts, redis asyncio ────────────────────────


def test_timeouts_del_camino_de_peticion_hasta_100ms(router_src):
    tree = ast.parse(router_src)
    constantes = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", "").endswith("TIMEOUT_SECONDS"):
                    constantes[target.id] = node.value.value
    for nombre in ("REDIS_OP_TIMEOUT_SECONDS", "CONFIG_TIMEOUT_SECONDS"):
        assert constantes[nombre] <= 0.1, f"{nombre} = {constantes[nombre]} > 100 ms"
    # 25-09-2026: el sidecar salió del camino de la petición. Su agregado tarda
    # ~160 ms (DNS de pares vía relay) y con el tope síncrono de 100 ms fallaba
    # SIEMPRE: cada pod solo se contaba a sí mismo. Ahora lo lee un sondeo en
    # background; SIDECAR_POLL_TIMEOUT_SECONDS puede ser holgado SOLO porque
    # _inflight_resident no hace NINGUNA I/O de red (se comprueba abajo).
    assert "SIDECAR_TIMEOUT_SECONDS" not in constantes
    assert 0.1 < constantes["SIDECAR_POLL_TIMEOUT_SECONDS"] <= 5.0
    inflight = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_inflight_resident"
    )
    inflight_src = ast.get_source_segment(router_src, inflight)
    assert "httpx" not in inflight_src and "SIDECAR_URL" not in inflight_src
    # 21-09 (post-deploy): CONFIG_REFRESH_TIMEOUT_SECONDS puede ser holgado SOLO
    # porque vive fuera del camino de la petición — _config() no hace NINGUNA I/O
    # (stale-while-revalidate). El backend tarda 130-300 ms (subprocess kubectl)
    # y un fetch síncrono con 100 ms timeoutearía siempre: el panel no propagaría.
    assert 0.1 < constantes["CONFIG_REFRESH_TIMEOUT_SECONDS"] <= 5.0
    # 21-09 (fix defecto 1 QA live): STICKY_WRITE_TIMEOUT_SECONDS también puede
    # ser holgado SOLO porque la escritura sticky es fire-and-forget — _sticky_set
    # programa _sticky_set_bg y devuelve el control al instante. Con 100 ms en el
    # camino de la petición ~99% de las escrituras morían bajo carga.
    assert 0.1 < constantes["STICKY_WRITE_TIMEOUT_SECONDS"] <= 5.0
    sticky_set = next(n for n in tree.body
                      if isinstance(n, ast.AsyncFunctionDef) and n.name == "_sticky_set")
    src_sticky_set = ast.get_source_segment(router_src, sticky_set)
    assert "wait_for" not in src_sticky_set and "_schedule(" in src_sticky_set, \
        "_sticky_set no puede bloquear la petición: fire-and-forget obligatorio"
    fondo = next(n for n in tree.body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "_sticky_set_bg")
    assert "STICKY_WRITE_TIMEOUT_SECONDS" in ast.get_source_segment(router_src, fondo)
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_config")
    src_config = ast.get_source_segment(router_src, fn)
    assert "httpx" not in src_config and ".get(" not in src_config, \
        "_config() no puede bloquearse en I/O de red (solo lecturas de caché)"
    refresco = next(n for n in tree.body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_refresh_config")
    assert "CONFIG_REFRESH_TIMEOUT_SECONDS" in ast.get_source_segment(router_src, refresco)


def test_redis_asyncio(router_src):
    assert "import redis.asyncio" in router_src
    assert "socket_timeout" in router_src


def test_redis_pool_caliente_y_warmup(router_src):
    """Fix defecto 1: el pool mantiene la conexión viva (health_check_interval)
    y la primera (DNS+TCP+AUTH) se paga en background, fuera de la petición."""
    assert "health_check_interval" in router_src
    assert "_redis_warmup" in router_src


def test_socket_del_cliente_no_topa_a_100ms(router_src):
    """Medido en vivo 21-09: DNS=82 ms + conectar+AUTH en frío ≈ 220 ms. Con
    socket_timeout=REDIS_OP_TIMEOUT_SECONDS ninguna conexión fría llegaba jamás,
    ni la de la escritura en background (su wait_for de 2 s no aplicaba: el
    socket moría antes a los 100 ms). El cliente usa el presupuesto holgado;
    los 100 ms del camino de la petición los impone el wait_for de _sticky_get."""
    redis_fn = next(n for n in ast.parse(router_src).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_redis")
    src_redis = ast.get_source_segment(router_src, redis_fn)
    assert "socket_timeout=STICKY_WRITE_TIMEOUT_SECONDS" in src_redis
    assert "socket_connect_timeout=STICKY_WRITE_TIMEOUT_SECONDS" in src_redis
    get_fn = next(n for n in ast.parse(router_src).body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "_sticky_get")
    src_get = ast.get_source_segment(router_src, get_fn)
    assert "timeout=REDIS_OP_TIMEOUT_SECONDS" in src_get, \
        "el camino de petición mantiene su presupuesto de 100 ms vía wait_for"


def test_fallos_de_valkey_dejan_rastro_throttled(router_src):
    """Fix defecto 3 + observabilidad: sticky_get/sticky_set/warmup ya no
    fallan en silencio, y los warnings van throttled (1/min con cuenta) para
    no inundar el log bajo carga (930 líneas en 40 min midió el QA live)."""
    for nombre in ("_sticky_get", "_sticky_set_bg", "_redis_warmup"):
        fn = next(n for n in ast.parse(router_src).body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == nombre)
        assert "_warn_throttled" in ast.get_source_segment(router_src, fn), nombre
    assert 'def _warn_throttled' in router_src


def test_decision_deja_una_linea_observable(router_src):
    """Fix defecto 2: cada petición con features activos o reescritura produce
    UNA línea INFO con model/sid/bound/plan/cool/inflight/decision — los
    caminos degradados de alibaba ya no son mudos."""
    apply_fn = next(n for n in ast.parse(router_src).body
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "apply_session_routing")
    src = ast.get_source_segment(router_src, apply_fn)
    assert "session_router decision:" in src
    for campo in ("cool=", "plan=", "bound=", "inflight=", "sid="):
        assert campo in src, campo
    # El proceso proxy filtra los INFO de este logger (medido en vivo 21-09):
    # toda decisión notable (reescritura/degradación) debe salir por WARNING.
    assert "log.warning" in src and "degradado" in src
    rewrite_fn = next(n for n in ast.parse(router_src).body
                      if isinstance(n, ast.FunctionDef) and n.name == "_rewrite")
    assert "log.warning" in ast.get_source_segment(router_src, rewrite_fn)


def test_apply_session_routing_es_fail_open(router_src):
    tree = ast.parse(router_src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "apply_session_routing"
    )
    # try/except que devuelve False: ningún error puede propagarse al hook
    assert any(isinstance(n, ast.Try) for n in fn.body)
    assert any(
        isinstance(n, ast.Return) and getattr(n.value, "value", None) is False
        for n in ast.walk(fn)
    )


def test_config_stale_while_revalidate(router_src):
    """SWR (21-09): caché con TTL + refresco en background single-flight. El
    camino de la petición devuelve SIEMPRE al instante (caché o defaults); el
    único que hace I/O es _refresh_config, y renueva el TTL incluso en fallo
    para no lanzar una tormenta de tasks contra el backend."""
    assert "CONFIG_TTL_SECONDS" in router_src
    tree = ast.parse(router_src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_config")
    src = ast.get_source_segment(router_src, fn)
    assert "create_task(_refresh_config())" in src  # refresco en background
    assert ".done()" in src                          # single-flight: uno a la vez
    assert "await " not in src.replace("async def _config():", ""), \
        "_config no espera nada: sirve la caché y devuelve el control"
    refresco = next(n for n in tree.body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_refresh_config")
    cuerpo = ast.get_source_segment(router_src, refresco)
    # el rinnovo del TTL va FUERA del try (se ejecuta también cuando el fetch falla)
    assert cuerpo.rstrip().endswith('_config_cache["expires"] = time.monotonic() + CONFIG_TTL_SECONDS')
    assert any(isinstance(n, ast.Try) for n in refresco.body)  # nunca lanza


# ── Mandato 7: comentario de router_settings actualizado ─────────────────────


def test_comentario_afinidad_actualizado():
    texto = MANIFEST.read_text()
    assert "NO hay afinidad por conversacion" not in texto
    assert "INFRA-208" in texto


# ── Forma del manifiesto (mandato 10: valkey + ExternalSecret + Service) ─────


def test_valkey_deployment_con_requirepass(docs):
    dep = next(
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm-valkey"
    )
    container = dep["spec"]["template"]["spec"]["containers"][0]
    args = container["args"]
    i = args.index("--requirepass")
    assert args[i + 1] == "$(VALKEY_PASSWORD)"
    env = {e["name"]: e for e in container["env"]}
    ref = env["VALKEY_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == "litellm-valkey-creds" and ref["key"] == "password"
    assert "optional" not in ref  # sin contraseña el valkey NO arranca
    volumes = dep["spec"]["template"]["spec"].get("volumes") or []
    assert not [v for v in volumes if "persistentVolumeClaim" in v]  # efímero


def test_external_secret_valkey(docs):
    eso = next(
        d for d in docs
        if d.get("kind") == "ExternalSecret"
        and d["metadata"]["name"] == "litellm-valkey-creds"
    )
    ref = eso["spec"]["data"][0]["remoteRef"]["key"]
    assert ref == "litellm/VALKEY_PASSWORD"
    assert eso["spec"]["secretStoreRef"]["name"] == "onepassword"


def test_service_valkey(docs):
    svc = next(
        d for d in docs
        if d.get("kind") == "Service" and d["metadata"]["name"] == "litellm-valkey"
    )
    assert svc["spec"]["ports"][0]["port"] == 6379


def test_litellm_env_y_mount_del_hook(docs):
    dep = next(
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm"
    )
    container = dep["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    assert "SESSION_ROUTER_REDIS_URL" in env
    assert "MODEL_ROUTING_CONFIG_URL" in env
    pwd = env["SESSION_ROUTER_REDIS_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert pwd["name"] == "litellm-valkey-creds" and pwd.get("optional") is True
    mounts = container["volumeMounts"]
    assert any(
        m.get("subPath") == "session_router.py"
        and m["mountPath"] == "/app/session_router.py"
        for m in mounts
    )


# ── Mandato 6: precedencia, en comportamiento ────────────────────────────────


def _cfg(**over):
    base = {
        "sticky": False, "instant_reject": False, "local_slots": 8,
        "default_plan": "local", "session_plans": {},
    }
    base.update(over)
    return base


class _Env:
    """Stubbea las cuatro E/S del módulo (config, sticky, cooldown, contador)
    y graba lo que se escribe en Valkey."""

    def __init__(self, mod, cfg, bound=None, cooldown=False, inflight=0, stuck=False):
        self.writes = []
        self.mod = mod
        mod._ensure_vllm_poller = lambda: None
        mod._queue_stuck = lambda now=None: stuck

        async def config():
            return cfg

        async def sticky_get(sid):
            return bound

        async def sticky_set(sid, plan):
            self.writes.append((sid, plan))

        def group_in_cooldown(name):
            return cooldown

        async def inflight_resident(tracker):
            return inflight

        mod._config = config
        mod._sticky_get = sticky_get
        mod._sticky_set = sticky_set
        mod._group_in_cooldown = group_in_cooldown
        mod._inflight_resident = inflight_resident

    def run(self, data, requested="tooling", resident_ready=True):
        return asyncio.run(self.mod.apply_session_routing(
            data, requested, tracker=object(), resident_ready=resident_ready,
        ))


def _data(model=RESIDENT, **extra):
    base = {"model": model, "litellm_trace_id": SID, "metadata": {}}
    base.update(extra)
    return base


def test_flags_apagados_noop_exacto(router_mod):
    env = _Env(router_mod, _cfg(), inflight=99)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT and env.writes == []


def test_sellado_gana_a_todo(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0,
                                default_plan="alibaba"), cooldown=False)
    data = _data(disable_fallbacks=True)
    assert env.run(data) is False
    assert data["model"] == RESIDENT and env.writes == []


@pytest.mark.parametrize("pedido", ["tooling-uncensored", "qwen38-u-off"])
def test_uncensored_por_nombre_pedido_no_cae_a_alibaba(router_mod, pedido):
    env = _Env(router_mod, _cfg(sticky=True, default_plan="alibaba"))
    data = _data()
    assert env.run(data, requested=pedido) is False
    assert data["model"] == RESIDENT


def test_uncensored_resuelto_pasa_de_largo(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0))
    data = _data(model="qwen38-flash-next-uncensored")
    assert env.run(data) is False
    assert data["model"] == "qwen38-flash-next-uncensored"


def test_plan_explicito_local_nunca_se_rechaza(router_mod):
    """Mandato 6: el plan explícito es decisión del usuario — cola, no válvula."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0,
                                session_plans={SID: "local"}), inflight=99)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert env.writes == []  # sticky no se escribe para planes explícitos


def test_plan_explicito_claude_es_noop_en_el_hook(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0,
                                session_plans={SID: "claude"}), inflight=99)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT


def test_plan_explicito_alibaba_reescribe_y_marca(router_mod):
    env = _Env(router_mod, _cfg(sticky=False, instant_reject=False,
                                session_plans={SID: "alibaba"}))
    data = _data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert data["metadata"]["_session_routing_rerouted"] is True
    assert data["metadata"]["_router_original_model"] == RESIDENT
    assert "api_base" not in data and "api_key" not in data


def test_plan_explicito_alibaba_con_cooldown_degrada(router_mod):
    env = _Env(router_mod, _cfg(session_plans={SID: "alibaba"}), cooldown=True)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT


def test_sticky_default_alibaba_vincula_y_reescribe(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, default_plan="alibaba"))
    data = _data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]


def test_sticky_binding_local_conserva_residente(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False), bound="local")
    data = _data()
    assert env.run(data) is False
    # 23-09: el vínculo se RENUEVA en cada petición (TTL = inactividad)
    assert data["model"] == RESIDENT and env.writes == [(SID, "local")]


def test_sticky_alibaba_ligada_renueva_el_vinculo(router_mod):
    env = _Env(router_mod, _cfg(sticky=True), bound="alibaba")
    data = _data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]


def test_ttl_de_inactividad_por_destino(router_mod):
    """26-09-2026 (Dani): local 30 min, alibaba 5 min (antes 10 min los dos)."""
    assert router_mod.STICKY_TTL_LOCAL_SECONDS == 1800
    assert router_mod.STICKY_TTL_ALIBABA_SECONDS == 300
    assert router_mod._sticky_ttl("local") == 1800
    assert router_mod._sticky_ttl("alibaba") == 300


def test_escritura_sticky_usa_el_ttl_de_su_destino(fresh_mod, monkeypatch):
    m = fresh_mod
    escritas = []

    class Cli:
        async def set(self, key, value, ex=None):
            escritas.append((key, value, ex))

    async def redis():
        return Cli()

    monkeypatch.setattr(m, "_redis", redis)
    asyncio.run(m._sticky_set_bg("s1", "local"))
    asyncio.run(m._sticky_set_bg("s2", "alibaba"))
    assert escritas == [
        (m.STICKY_KEY_PREFIX + "s1", "local", 1800),
        (m.STICKY_KEY_PREFIX + "s2", "alibaba", 300),
    ]


def test_sticky_local_con_hueco_se_revincula_a_alibaba(router_mod):
    """compute-mode no admite residente => rebind al destino sano."""
    env = _Env(router_mod, _cfg(sticky=True), bound="local")
    data = _data()
    assert env.run(data, resident_ready=False) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]


def test_sticky_alibaba_en_cooldown_se_revincula_a_local(router_mod):
    env = _Env(router_mod, _cfg(sticky=True), bound="alibaba", cooldown=True)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert env.writes == [(SID, "local")]


def test_valvula_sin_sticky_reescribe_al_instante(router_mod):
    env = _Env(router_mod, _cfg(sticky=False, instant_reject=True, local_slots=2),
               inflight=2)
    data = _data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert data["metadata"]["_session_routing_reason"] == "instant_reject"
    assert env.writes == []  # sin sticky no se escribe binding


def test_valvula_por_debajo_de_slots_no_actua(router_mod):
    env = _Env(router_mod, _cfg(instant_reject=True, local_slots=8), inflight=7)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT


def test_valvula_con_cooldown_desconocido_degrada(router_mod):
    """Mandato 5: cooldown None (no se pudo saber) => ni rewrite ni stick a
    Alibaba. El binding al destino LOCAL sano sí se escribe (no depende del
    cooldown de Alibaba)."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0),
               cooldown=None, inflight=99)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert env.writes == [(SID, "local")]


def test_peticion_al_grupo_alibaba_no_se_toca(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0),
               inflight=99)
    data = _data(model=OVERFLOW)
    assert env.run(data) is False
    assert data["model"] == OVERFLOW


def test_modelos_ajenos_pasan_de_largo(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0),
               inflight=99)
    for modelo in ("claude-opus-4", "or-gpt5", "qwen38-27b", "tooling"):
        data = _data(model=modelo)
        assert env.run(data) is False
        assert data["model"] == modelo


def test_sin_sid_no_hay_sticky_pero_la_valvula_actua(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=1),
               inflight=1)
    data = {"model": RESIDENT, "metadata": {}}
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == []  # sin sid no se puede vincular


def test_fallo_interno_devuelve_false_sin_tocar_data(router_mod):
    async def boom():
        raise RuntimeError("config caída")

    anterior = router_mod._config
    router_mod._config = boom  # apply envuelve TODO en try/except
    try:
        data = _data()
        assert asyncio.run(router_mod.apply_session_routing(
            data, "tooling", tracker=None, resident_ready=True)) is False
        assert data["model"] == RESIDENT
    finally:
        router_mod._config = anterior


# ── INFRA-208 (22-09): clase de sesión x-claude-class (C1-C4) ───────────────
# Decisión de Dani 21-09: las sesiones de la compañía (cabecera que estampa el
# wrapper de x86, publisher en x86-host-runtime — PR aparte) NUNCA saltan a
# Alibaba por la válvula: deciden `local` y la admisión de strip_params encola.
# Las demás sesiones siguen saltando igual que hoy. Plan y vía real verificada:
# plans/company-class-instant-reject-plan.md.


def _company_data(**extra):
    """data como la que entrega add_litellm_data_to_request en litellm
    v1.100.0: las cabeceras del request, con la caja del cable, viven en
    data["metadata"]["headers"] (asignación incondicional; verificado contra
    la versión pineada y con sonda en vivo el 22-09)."""
    return _data(metadata={"headers": {"x-claude-class": "company"}}, **extra)


# C1: company + saturado + instant_reject=true => encola, cero reescrituras,
# cero re-bind sticky a alibaba.


def test_c1_company_con_alibaba_desactivado_encola_sin_reescribir(router_mod):
    """Interruptor «Fallback Alibaba» desactivado: la compañía no desborda aunque la
    válvula general esté encendida y el residente lleno (en producción además viene
    sellada por apply_company_policy; aquí se prueba la válvula sola)."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2,
                                company={"claude": True, "alibaba": False}), inflight=99)
    data = _company_data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert "_session_routing_rerouted" not in data["metadata"]
    assert env.writes == [(SID, "local")]


def test_c1_company_con_alibaba_activado_desborda_aunque_instant_reject_este_apagado(router_mod):
    """23-09-2026 (deroga el "la compañía nunca salta" del 21-09): con su interruptor
    activado, la compañía desborda a Alibaba al llenarse el residente, gobernada por
    SU interruptor y no por instant_reject (que sigue siendo el del resto)."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=2,
                                company={"claude": True, "alibaba": True}), inflight=2)
    data = _company_data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert data["metadata"]["_session_routing_reason"] == "company_overflow"
    assert env.writes == [(SID, "alibaba")]  # la sesión se muda (afinidad de caché)


def test_c1_company_sin_campo_company_es_activado(router_mod):
    """Config sin el campo `company` (dashboard viejo): el interruptor cuenta como activado."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=2), inflight=5)
    data = _company_data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW


def test_c1_company_con_hueco_se_queda_en_local(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=16), inflight=15)
    data = _company_data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert env.writes == [(SID, "local")]


def test_c1_company_sin_sticky_desborda_por_peticion_sin_escribir(router_mod):
    """Con sticky apagado e instant_reject apagado la compañía sigue teniendo su válvula:
    por petición, sin binding."""
    env = _Env(router_mod, _cfg(sticky=False, instant_reject=False, local_slots=2), inflight=99)
    data = _company_data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == []


def test_c1_company_con_alibaba_en_cooldown_encola(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=2),
               inflight=99, cooldown=True)
    data = _company_data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT


@pytest.mark.parametrize("clave,valor", [
    ("X-Claude-Class", "company"),   # caja del cable sobre HTTP/1.1
    ("x-CLAUDE-class", "Company"),   # SDKs que canonizan mayúsculas intermedias
    ("X-CLAUDE-CLASS", " COMPANY "), # recorte del valor también
])
def test_c1_company_case_insensitive_por_clave_y_valor(router_mod, clave, valor):
    """clean_headers (v1.100.0) guarda las claves con la caja del cable: un
    get() a pelo por la minúscula sería el bug. La lectura es case-insensitive.
    Con instant_reject apagado solo la compañía desborda: si salta, se reconoció."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=2),
               inflight=99)
    data = _data(metadata={"headers": {clave: valor}})
    assert env.run(data) is True
    assert data["model"] == OVERFLOW


def test_c1_sin_clase_e_instant_reject_apagado_no_desborda(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=2), inflight=99)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT


# C2: sin cabecera (o con otra clase) => comportamiento idéntico al actual.


def test_c2_sin_cabecera_la_valvula_sigue_saltando_y_rebinde(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = _data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]  # la sesión se muda (afinidad de caché)
    assert data["metadata"]["_session_routing_rerouted"] is True


@pytest.mark.parametrize("otro_valor", ["interactive", "compania", "", "   "])
def test_c2_otras_clases_comportamiento_actual(router_mod, otro_valor):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = _data(metadata={"headers": {"x-claude-class": otro_valor}})
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]  # la sesión se muda (afinidad de caché)


# C3: la cabecera no esquiva sellado, uncensored, plan explícito, sticky ya
# ligado a alibaba ni el re-bind por residente no-ready — solo desactiva la
# válvula; y no salta cola (la supresión devuelve el control a la admisión).


def test_c3_company_no_esquiva_el_sellado(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0,
                                default_plan="alibaba"), inflight=99)
    data = _company_data(disable_fallbacks=True)
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert env.writes == []


def test_c3_company_no_esquiva_uncensored(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, default_plan="alibaba"), inflight=99)
    data = _company_data()
    assert env.run(data, requested="tooling-uncensored") is False
    assert data["model"] == RESIDENT


def test_c3_plan_exPLICITO_alibaba_gana_a_la_clase(router_mod):
    """Orden del operador > clase: el plan explícito sigue reescribiendo."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0,
                                session_plans={SID: "alibaba"}), inflight=99)
    data = _company_data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW


def test_c3_sticky_ya_ligado_a_alibaba_sigue_ligado(router_mod):
    """Binding previo (default_plan=alibaba, o escrito antes de desplegar,
    TTL 1 h): company solo apaga la VALVULA, no deshace destinos."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0),
               bound="alibaba", inflight=99)
    data = _company_data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW


def test_c3_residente_no_ready_sigue_rebindeando(router_mod):
    """No es saturación, es que el residente no está: encolar sería un 503
    seguro. La ruta de disponibilidad (compute-mode) aplica igual a company."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=0),
               bound="local", inflight=99)
    data = _company_data()
    assert env.run(data, resident_ready=False) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]


# C4: fail-open — cualquier forma rara de la cabecera/metadata = sin clase =
# comportamiento actual, sin excepción.


@pytest.mark.parametrize("cabeceras", ["no-dict", 42, None,
                                       ["x-claude-class", "company"]])
def test_c4_cabeceras_con_forma_rara_fail_open(router_mod, cabeceras):
    env = _Env(router_mod, _cfg(sticky=False, instant_reject=True, local_slots=2),
               inflight=99)
    data = _data(metadata={"headers": cabeceras})
    assert env.run(data) is True  # sin excepción y saltando como hoy
    assert data["model"] == OVERFLOW


def test_c4_metadata_no_dict_comportamiento_identico(router_mod):
    """metadata corrupta (ni dict): el helper no lanza y la petición se
    comporta EXACTAMENTE igual con y sin cabecera."""
    for with_class in (False, True):
        env = _Env(router_mod, _cfg(sticky=False, instant_reject=True,
                                    local_slots=2), inflight=99)
        data = {"model": RESIDENT, "litellm_trace_id": SID, "metadata": "no-dict"}
        if with_class:
            data["headers"] = {"x-claude-class": "company"}
        assert env.run(data) is True
        assert data["model"] == OVERFLOW


def test_c4_metadata_lista_no_dict_fail_open(router_mod):
    """metadata lista (ni dict): el helper y _rewrite la toleran; el sid sale
    del trace_id y la válvula actúa EXACTAMENTE como sin cabecera."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = {"model": RESIDENT, "litellm_trace_id": SID, "metadata": ["headers"]}
    assert env.run(data) is True  # sin clase => comportamiento actual
    assert data["model"] == OVERFLOW
    assert data["metadata"]["_session_routing_rerouted"] is True  # _rewrite la reemplazó


# Observabilidad y forma: la supresión es grepeable y el gate no se ensancha.


def test_class_aparece_en_la_linea_de_decision(router_mod, caplog):
    import logging

    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=2,
                                company={"claude": True, "alibaba": True}), inflight=99)
    data = _company_data()
    with caplog.at_level(logging.INFO, logger="session_router"):
        assert env.run(data) is True
    lineas = [r.getMessage() for r in caplog.records
              if "session_router decision:" in r.getMessage()]
    # el desborde de la compañía es grepeable: class=company y la razón propia.
    assert any("class=company" in l and "company_overflow" in l for l in lineas), lineas


def test_sin_cabecera_class_sale_como_guion(router_mod, caplog):
    import logging

    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False), inflight=0)
    data = _data()
    with caplog.at_level(logging.INFO, logger="session_router"):
        assert env.run(data) is False
    lineas = [r.getMessage() for r in caplog.records
              if "session_router decision:" in r.getMessage()]
    assert any("class=-" in l for l in lineas), lineas


def test_gate_de_valvula_exacto_y_helper_robusto(router_src):
    """La válvula: la compañía por SU interruptor, el resto por instant_reject, y
    siempre solo sobre el residente (no se ensancha a otras rutas). La lectura de la
    clase es case-insensitive por clave y el marcador de contrato está en el sitio."""
    assert ('valvula = (company and company_overflow) or (not company and config["instant_reject"])'
            in router_src)
    assert "if valvula and model == RESIDENT_MODEL:" in router_src
    tree = ast.parse(router_src)
    cls_fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_claude_class")
    src = ast.get_source_segment(router_src, cls_fn)
    assert "key.lower() == CLASS_HEADER" in src
    assert "isinstance(source, dict)" in src  # formas raras => None, sin lanza
    apply_fn = next(n for n in tree.body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_apply")
    src_apply = ast.get_source_segment(router_src, apply_fn)
    # company solo decide la válvula (y el corte de flags apagados): ninguna otra ruta lo mira
    assert src_apply.count("not company and") == 1, "company no debe colarse en más caminos"
    # definición, corte de flags apagados, válvula y razón del log: nada más
    assert src_apply.count("company_overflow") == 4
    assert "# CONTRACT: dgx.claude.class-header.v1" in router_src


# ── Interruptor «Fallback Alibaba» de la compañía (23-09-2026) ────────────────
# El panel Settings de dgx.e-dani.com/claude-sessions guarda el interruptor en
# control-nexus/company-control; el dashboard lo sirve en el campo ADITIVO
# `company` de /api/model-routing/config. Con alibaba=false una petición de la
# compañía se sella (nada la lleva a Alibaba) y un `alibaba-*` explícito es 403.
# El resto de clientes no cambia.


def _policy(mod, cfg, data, requested="tooling"):
    async def config():
        return cfg
    mod._config = config
    return asyncio.run(mod.apply_company_policy(data, requested))


def test_company_sanitize_solo_un_false_bool_apaga(router_mod):
    assert router_mod._sanitize({})["company"] == {"claude": True, "alibaba": True}
    assert router_mod._sanitize({"company": {"alibaba": "false", "claude": 0}})["company"] == {
        "claude": True, "alibaba": True}
    assert router_mod._sanitize({"company": {"alibaba": False}})["company"] == {
        "claude": True, "alibaba": False}
    assert router_mod._sanitize({"company": ["basura"]})["company"] == {"claude": True, "alibaba": True}
    assert router_mod.DEFAULT_CONFIG["company"] == {"claude": True, "alibaba": True}


def test_company_con_alibaba_permitido_no_toca_nada(router_mod):
    data = _company_data()
    assert _policy(router_mod, _cfg(company={"claude": True, "alibaba": True}), data) == (False, None)
    assert "disable_fallbacks" not in data
    # config vieja sin el campo: igual (fail-open al comportamiento anterior)
    data = _company_data()
    assert _policy(router_mod, _cfg(), data) == (False, None)
    assert "disable_fallbacks" not in data


def test_company_sin_alibaba_sella_la_peticion(router_mod):
    data = _company_data()
    assert _policy(router_mod, _cfg(company={"claude": True, "alibaba": False}), data) == (False, None)
    assert data["disable_fallbacks"] is True


def test_company_sin_alibaba_rechaza_un_alibaba_explicito(router_mod):
    for pedido in ("alibaba-q38-max", "alibaba-qwen38-max", "ALIBABA-q38-flash"):
        data = _company_data(model=pedido)
        denegada, detalle = _policy(router_mod, _cfg(company={"alibaba": False}), data, requested=pedido)
        assert denegada is True
        assert detalle["error"] == "company_alibaba_disabled"
        assert "disable_fallbacks" not in data


def test_sin_clase_company_el_interruptor_no_aplica(router_mod):
    for data in (_data(), _data(metadata={"headers": {"x-claude-class": "interactive"}}),
                 _data(model="alibaba-q38-max")):
        assert _policy(router_mod, _cfg(company={"claude": False, "alibaba": False}), data,
                       requested=data["model"]) == (False, None)
        assert "disable_fallbacks" not in data


def test_company_sellada_no_se_reescribe_a_alibaba(router_mod):
    """El sello es lo que ya respeta el hook (mandato 4a): con alibaba=false, ni el
    plan explícito alibaba, ni un sticky ligado a alibaba, ni el re-bind por
    residente no-ready sacan a la compañía del local."""
    cfg = _cfg(sticky=True, instant_reject=True, local_slots=0, default_plan="alibaba",
               session_plans={SID: "alibaba"}, company={"claude": True, "alibaba": False})
    env = _Env(router_mod, cfg, bound="alibaba", inflight=99)
    data = _company_data()
    _policy(router_mod, cfg, data)
    env = _Env(router_mod, cfg, bound="alibaba", inflight=99)
    assert env.run(data, resident_ready=False) is False
    assert data["model"] == RESIDENT
    assert env.writes == []


def test_company_policy_fail_open(router_mod):
    async def revienta():
        raise RuntimeError("panel caído")
    router_mod._config = revienta
    data = _company_data()
    assert asyncio.run(router_mod.apply_company_policy(data, "tooling")) == (False, None)
    assert "disable_fallbacks" not in data


def test_strip_params_llama_a_la_politica_de_la_compania(strip_src):
    """Justo tras la política de fallbacks por key, tolerando un módulo sin la
    función, y con 403 visible para el alibaba explícito."""
    i_key = strip_src.index("fallbacks_disabled = _apply_key_fallback_policy(data, user_api_key_dict)")
    i_pol = strip_src.index('getattr(session_router, "apply_company_policy", None)')
    i_sr = strip_src.index("session_router.apply_session_routing(")
    assert i_key < i_pol < i_sr
    bloque = strip_src[i_pol:i_pol + 600]
    assert "raise HTTPException(status_code=403, detail=_detail)" in bloque


def test_company_por_v1_messages_lee_litellm_metadata(router_mod):
    """23-09-2026, medido en vivo: en /v1/messages (LITELLM_METADATA_ROUTES de
    litellm) las cabeceras van a data["litellm_metadata"]["headers"]. Es la ruta
    de Claude Code entera: sin leerla, la compañía no se reconocía por ahí."""
    data = {"model": RESIDENT, "litellm_trace_id": SID,
            "litellm_metadata": {"headers": {"X-Claude-Class": "company"}}}
    assert router_mod._claude_class(data) == "company"
    assert _policy(router_mod, _cfg(company={"claude": True, "alibaba": False}), data) == (False, None)
    assert data["disable_fallbacks"] is True
    data = {"model": "alibaba-q38-max", "litellm_metadata": {"headers": {"x-claude-class": "company"}}}
    denegada, detalle = _policy(router_mod, _cfg(company={"alibaba": False}), data, requested="alibaba-q38-max")
    assert denegada is True and detalle["error"] == "company_alibaba_disabled"



# ── Sin esperas (23-09-2026): presupuesto único + cola de vLLM + desvío por petición ──


def test_cola_de_vllm_atascada_muda_la_sesion(router_mod):
    """Con pocas en vuelo pero la cola de vLLM atascada >= tolerancia, la sesión sale a
    Alibaba y se QUEDA allí (sticky): desviar petición a petición la dejaba fría en los
    dos lados (medido el 23-09: 22 % de acierto en Alibaba)."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=16),
               inflight=3, stuck=True, bound="local")
    data = _company_data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert data["metadata"]["_session_routing_trigger"] == "cola"
    assert env.writes == [(SID, "alibaba")]


def test_cola_sin_dato_fresco_no_desvia(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False, local_slots=16),
               inflight=3, stuck=None, bound="local")
    data = _company_data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT


def test_lleno_marca_el_motivo(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=4),
               inflight=4, bound="local")
    data = _data()
    assert env.run(data) is True
    assert data["metadata"]["_session_routing_trigger"] == "lleno"
    assert data["metadata"]["_session_routing_reason"] == "instant_reject"


def test_cola_atascada_no_desvia_a_quien_no_tiene_valvula(router_mod):
    """Sin clase company e instant_reject apagado: ni con la cola atascada."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=False), inflight=1, stuck=True)
    data = _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT


def test_parse_vllm_gauges_suma_engines_y_filtra_modelo(router_mod):
    texto = "\n".join([
        "# HELP vllm:num_requests_waiting x",
        'vllm:num_requests_waiting{engine="0",model_name="qwen38-flash-next"} 2.0',
        'vllm:num_requests_waiting{engine="1",model_name="qwen38-flash-next"} 1.0',
        'vllm:num_requests_running{engine="0",model_name="qwen38-flash-next"} 9.0',
        'vllm:num_requests_waiting{engine="0",model_name="otro"} 50.0',
        'vllm:num_requests_waiting_by_reason{model_name="qwen38-flash-next",reason="capacity"} 7.0',
    ])
    assert router_mod._parse_vllm_gauges(texto, "qwen38-flash-next") == (3.0, 9.0)
    assert router_mod._parse_vllm_gauges("nada", "qwen38-flash-next") == (None, None)


@pytest.fixture
def fresh_mod(router_src):
    """Módulo recién cargado: _Env deja stubs en el módulo compartido (scope=module)."""
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        module = types.ModuleType("session_router_fresh")
        exec(compile(router_src, "session_router.py", "exec"), module.__dict__)
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)
    return module


def test_queue_stuck_cuenta_desde_que_la_cola_no_se_vacia(fresh_mod):
    m = fresh_mod
    tol = m.WAIT_TOLERANCE_SECONDS
    assert tol == 5.0
    assert m._queue_stuck(now=1.0) is None                       # sin lecturas: no actúa
    m._note_vllm_sample(2, 10, now=100.0)
    assert m._queue_stuck(now=100.0 + tol - 0.1) is False
    m._note_vllm_sample(1, 10, now=102.0)                        # sigue sin vaciarse
    assert m._queue_stuck(now=100.0 + tol) is True
    m._note_vllm_sample(0, 10, now=100.0 + tol + 1)              # se vació: se reinicia
    assert m._queue_stuck(now=100.0 + tol + 1) is False
    assert m._queue_stuck(now=100.0 + tol + 1 + m.VLLM_STALE_SECONDS + 1) is None  # dato viejo


def test_presupuesto_unico_cuenta_toda_la_familia_del_residente(fresh_mod):
    """El abliterado va al MISMO vLLM: cuenta contra el mismo presupuesto."""
    m = fresh_mod
    assert m.RESIDENT_FAMILY == frozenset({RESIDENT, RESIDENT + "-uncensored"})

    class T:
        def snapshot(self):
            return {"a": {"model": RESIDENT}, "b": {"model": RESIDENT + "-uncensored"},
                    "c": {"model": OVERFLOW}}

    # 25-09-2026: las otras réplicas salen de la última lectura del sondeo en
    # background (agregado del sidecar menos /local), no de una llamada síncrona.
    m._ensure_sidecar_poller = lambda: None
    m._sidecar_remote["rows"] = [
        {"request_id": "a", "model": RESIDENT},                  # ya contado (este pod)
        {"request_id": "x", "model": RESIDENT + "-uncensored"},  # otra réplica
        {"request_id": "y", "model": OVERFLOW},
    ]
    m._sidecar_remote["read_at"] = m.time.monotonic()
    assert asyncio.run(m._inflight_resident(T())) == 3

    # Lectura vieja => solo este pod (fail-open, como con el sidecar mudo).
    m._sidecar_remote["read_at"] = m.time.monotonic() - m.SIDECAR_STALE_SECONDS - 1
    assert asyncio.run(m._inflight_resident(T())) == 2


def test_sondeo_del_sidecar_resta_las_filas_de_este_pod(fresh_mod, monkeypatch):
    """Una vuelta del sondeo: remoto = agregado menos /local del mismo ciclo."""
    m = fresh_mod

    class Resp:
        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            return None

        def json(self):
            return {"active": self.rows}

    class Cli:
        def __init__(self, *a, **k):
            pass

        async def get(self, url, **k):
            if url == m.SIDECAR_LOCAL_URL:
                return Resp([{"request_id": "a", "model": RESIDENT}])
            return Resp([
                {"request_id": "a", "model": RESIDENT},
                {"request_id": "x", "model": RESIDENT},
            ])

    class Stop(Exception):
        pass

    async def stop(*a, **k):
        raise Stop()

    monkeypatch.setattr(m.httpx, "AsyncClient", Cli, raising=False)
    monkeypatch.setattr(m.asyncio, "sleep", stop)
    with pytest.raises(Stop):
        asyncio.run(m._poll_sidecar_forever())
    assert [r["request_id"] for r in m._sidecar_remote["rows"]] == ["x"]
    assert m._sidecar_remote["read_at"] is not None




# ── Afinidad sin id de sesión (23-09-2026): opencode/hermes no mandan id estable ──


def _conv(first_user="haz X", extra=0, marca=False):
    sistema = {"type": "text", "text": "eres opencode"}
    if marca:
        sistema["cache_control"] = {"type": "ephemeral"}
    msgs = [{"role": "system", "content": [sistema]}, {"role": "user", "content": first_user}]
    for i in range(extra):
        msgs += [{"role": "assistant", "content": f"paso {i}"}, {"role": "user", "content": f"resultado {i}"}]
    return {"model": RESIDENT, "messages": msgs, "metadata": {"user_api_key_alias": "opencode"}}


def test_llave_de_afinidad_estable_en_toda_la_conversacion(fresh_mod):
    k1 = fresh_mod._prefix_affinity_key(_conv(extra=0))
    k2 = fresh_mod._prefix_affinity_key(_conv(extra=3))
    k3 = fresh_mod._prefix_affinity_key(_conv(extra=3, marca=True))   # cache_control movido
    assert k1 and k1.startswith("pfx-") and k1 == k2 == k3


def test_llave_de_afinidad_distingue_conversaciones_y_clientes(fresh_mod):
    a = fresh_mod._prefix_affinity_key(_conv("tarea A"))
    b = fresh_mod._prefix_affinity_key(_conv("tarea B"))
    c = _conv("tarea A"); c["metadata"] = {"user_api_key_alias": "hermes"}
    assert a != b and a != fresh_mod._prefix_affinity_key(c)


def test_llave_de_afinidad_forma_rara_es_none(fresh_mod):
    assert fresh_mod._prefix_affinity_key({"messages": "x"}) is None
    assert fresh_mod._prefix_affinity_key({}) is None


def test_sin_sid_el_desborde_vincula_por_la_llave_de_afinidad(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2), inflight=5)
    data = _conv()
    assert env.run(data) is True
    assert len(env.writes) == 1 and env.writes[0][0].startswith("pfx-") and env.writes[0][1] == "alibaba"


def test_con_sid_manda_el_sid(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2), inflight=5)
    data = _conv(); data["litellm_trace_id"] = SID
    env.run(data)
    assert env.writes == [(SID, "alibaba")]


# ── Interruptores «Fallbacks a Alibaba» (25-09-2026) ─────────────────────────
# Panel /inferencia (tarjeta ALIBABA · TOKEN PLAN) -> campo aditivo `alibaba` de
# /api/model-routing/config. Este módulo aplica `overflow` y expone los cuatro a
# strip_params por alibaba_switches().

ALIBABA_ON = {"router_fallback": True, "tooling_fallback": True,
              "demos_fallback": True, "overflow": True}


def test_alibaba_sanitize_solo_un_false_bool_apaga(router_mod):
    assert router_mod._sanitize({})["alibaba"] == ALIBABA_ON
    assert router_mod._sanitize({"alibaba": "off"})["alibaba"] == ALIBABA_ON
    assert router_mod._sanitize({"alibaba": {"overflow": 0, "router_fallback": "false"}})["alibaba"] == ALIBABA_ON
    assert router_mod._sanitize({"alibaba": {"overflow": False}})["alibaba"] == {**ALIBABA_ON, "overflow": False}
    assert router_mod.DEFAULT_CONFIG["alibaba"] == ALIBABA_ON


@pytest.mark.parametrize("cfg,company", [
    (_cfg(sticky=True, instant_reject=True, local_slots=0), False),
    (_cfg(default_plan="alibaba"), False),
    (_cfg(session_plans={SID: "alibaba"}), False),
    (_cfg(sticky=True, local_slots=0, company={"claude": True, "alibaba": True}), True),
])
def test_overflow_apagado_no_reescribe_por_ningun_camino(router_mod, cfg, company):
    env = _Env(router_mod, {**cfg, "alibaba": {**ALIBABA_ON, "overflow": False}}, inflight=99)
    data = _company_data() if company else _data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert "_session_routing_rerouted" not in data["metadata"]
    assert all(v != "alibaba" for _, v in env.writes)


def test_overflow_encendido_sigue_como_antes(router_mod):
    env = _Env(router_mod, {**_cfg(session_plans={SID: "alibaba"}), "alibaba": ALIBABA_ON}, inflight=0)
    data = _data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW


def test_alibaba_switches_lee_la_cache_y_hace_fail_open(router_mod):
    async def cfg_off():
        return router_mod._sanitize({"alibaba": {"router_fallback": False}})

    async def boom():
        raise RuntimeError("caída")

    anterior = router_mod._config
    try:
        router_mod._config = cfg_off
        assert asyncio.run(router_mod.alibaba_switches()) == {**ALIBABA_ON, "router_fallback": False}
        router_mod._config = boom
        assert asyncio.run(router_mod.alibaba_switches()) == ALIBABA_ON
    finally:
        router_mod._config = anterior


def test_strip_params_aplica_los_interruptores(strip_src):
    """Los tres de strip_params: demos vacía la lista por key, router estampa
    fallbacks:[] (sin sellar: el desborde tiene su propio interruptor) y tooling
    pasa base_fallbacks=() a la resolución del residente."""
    assert 'getattr(session_router, "alibaba_switches", None)' in strip_src
    assert 'not _alibaba_sw["demos_fallback"]' in strip_src
    i = strip_src.index('if not _alibaba_sw["router_fallback"] and "fallbacks" not in data:')
    assert strip_src[i:].split("\n")[1].strip() == 'data["fallbacks"] = []'
    assert "disable_fallbacks" not in strip_src[i:].split("\n")[1]
    assert 'base_fallbacks=None if _alibaba_sw["tooling_fallback"] else ()' in strip_src
