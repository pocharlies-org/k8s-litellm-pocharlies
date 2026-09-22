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
    for nombre in (
        "REDIS_OP_TIMEOUT_SECONDS", "SIDECAR_TIMEOUT_SECONDS", "CONFIG_TIMEOUT_SECONDS"
    ):
        assert constantes[nombre] <= 0.1, f"{nombre} = {constantes[nombre]} > 100 ms"
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

    def __init__(self, mod, cfg, bound=None, cooldown=False, inflight=0):
        self.writes = []
        self.mod = mod

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
    assert data["model"] == RESIDENT and env.writes == []


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


def test_c1_company_con_valvula_disparada_encola_sin_reescribir(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = _company_data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert "_session_routing_rerouted" not in data["metadata"]
    # fresh + sticky: el único binding escrito es LOCAL; ningún re-bind a
    # alibaba (lo que haría la válvula hoy).
    assert env.writes == [(SID, "local")]


def test_c1_company_sin_sticky_la_valvula_no_dispara(router_mod):
    env = _Env(router_mod, _cfg(sticky=False, instant_reject=True, local_slots=2),
               inflight=99)
    data = _company_data()
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert env.writes == []


@pytest.mark.parametrize("clave,valor", [
    ("X-Claude-Class", "company"),   # caja del cable sobre HTTP/1.1
    ("x-CLAUDE-class", "Company"),   # SDKs que canonizan mayúsculas intermedias
    ("X-CLAUDE-CLASS", " COMPANY "), # recorte del valor también
])
def test_c1_company_case_insensitive_por_clave_y_valor(router_mod, clave, valor):
    """clean_headers (v1.100.0) guarda las claves con la caja del cable: un
    get() a pelo por la minúscula sería el bug. La lectura es case-insensitive."""
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = _data(metadata={"headers": {clave: valor}})
    assert env.run(data) is False
    assert data["model"] == RESIDENT
    assert env.writes == [(SID, "local")]


# C2: sin cabecera (o con otra clase) => comportamiento idéntico al actual.


def test_c2_sin_cabecera_la_valvula_sigue_saltando_y_rebinde(router_mod):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = _data()
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]  # re-bind sticky intacto
    assert data["metadata"]["_session_routing_rerouted"] is True


@pytest.mark.parametrize("otro_valor", ["interactive", "compania", "", "   "])
def test_c2_otras_clases_comportamiento_actual(router_mod, otro_valor):
    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = _data(metadata={"headers": {"x-claude-class": otro_valor}})
    assert env.run(data) is True
    assert data["model"] == OVERFLOW
    assert env.writes == [(SID, "alibaba")]


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

    env = _Env(router_mod, _cfg(sticky=True, instant_reject=True, local_slots=2),
               inflight=99)
    data = _company_data()
    with caplog.at_level(logging.INFO, logger="session_router"):
        assert env.run(data) is False
    lineas = [r.getMessage() for r in caplog.records
              if "session_router decision:" in r.getMessage()]
    # con company la válvula ni se evalúa (no se cuenta inflight): la línea
    # muestra class=company y destino local — la supresión es grepeable.
    assert any("class=company" in l and "-> local" in l for l in lineas), lineas


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
    """El gate exige instant_reject + modelo residente + not company (no se
    ensancha a otras rutas), la lectura es case-insensitive por clave y el
    marcador de contrato está en el sitio."""
    assert 'config["instant_reject"] and model == RESIDENT_MODEL and not company' in router_src
    tree = ast.parse(router_src)
    cls_fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_claude_class")
    src = ast.get_source_segment(router_src, cls_fn)
    assert "key.lower() == CLASS_HEADER" in src
    assert "isinstance(source, dict)" in src  # formas raras => None, sin lanza
    apply_fn = next(n for n in tree.body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_apply")
    src_apply = ast.get_source_segment(router_src, apply_fn)
    # company solo se usa en el gate de la válvula: ninguna otra ruta lo mira
    assert src_apply.count("not company") == 1, "company no debe colarse en más caminos"
    assert "# CONTRACT: dgx.claude.class-header.v1" in router_src
