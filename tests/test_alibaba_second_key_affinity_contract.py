"""Contract de la segunda cuenta de Alibaba con afinidad por sesion (26-09-2026).

El cache de contexto de Model Studio es POR CUENTA (los dos planes Team no
comparten prefijo): repartir una conversacion entre las dos cuentas pagaria el
prefijo entero en cada salto. El reparto es por sesion, no por peticion, con el
mecanismo NATIVO del Router (DeploymentAffinityCheck, v1.100.0), y el hook le
alimenta el `session_id` propio (el del cliente no es estable).

Que vigila este contrato:

  1. Forma: cada grupo `alibaba-*` tiene exactamente DOS deployments; el `-k2`
     usa DASHSCOPE_API_KEY_2 y lleva `order: 2` (inerte hasta la activacion:
     failover puro, no reparto); ids estables `<grupo>-k1/-k2`; los flags de
     capacidad y los precios del pack van ESPEJADOS (un twin desincronizado es
     el riesgo del patron).
  2. router_settings: `model_group_affinity_config` activa session_affinity en
     exactamente los grupos `alibaba-*`, y TTL explicito.
  3. Hook: `stamp_alibaba_session_affinity` estampa el sid del hook SOLO en
     modelos `alibaba-*`, PISANDO el session_id del cliente, con el hash de
     prefijo como red de estabilidad; sin sid no escribe nada.
  4. El tier redis del DualCache se monta una vez por proceso, con timeout
     corto (el claim va en el camino de la peticion).
  5. Cableado: strip_params llama al stamp FUERA del gate de ROUTED_MODELS
     (un `alibaba-*` explicito nunca pasa por apply_session_routing).
  6. Plomada: ExternalSecret `litellm-alibaba-2` propio (aislado: un campo que
     falte no puede romper la key 1) y env `optional: true`.
"""
import ast
import sys
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

GROUPS = [
    "alibaba-q38-max", "alibaba-q38-flash", "alibaba-q37-plus", "alibaba-q37-max",
    "alibaba-q36-flash", "alibaba-dsv41-fl", "alibaba-dsv4-pro", "alibaba-dsv4f0731",
    "alibaba-glm-53", "alibaba-glm-52",
]

# Flags que el twin tiene que repetir SI o SI (riesgo de copia: se pudren).
MIRRORED_LITELLM_PARAMS = ("model", "api_base", "timeout", "num_retries")
MIRRORED_MODEL_INFO = (
    "mode", "backend", "supports_function_calling", "supports_vision",
    "supports_reasoning", "supported_reasoning_efforts", "cooldown_time",
    # Prorrateo del pack (dgx.litellm.alibaba-pack-pricing.v1): un twin sin
    # precios dejaria de facturar las peticiones clavadas en la cuenta 2.
    "input_cost_per_token", "cache_read_input_token_cost", "output_cost_per_token",
)


@pytest.fixture(scope="module")
def docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


@pytest.fixture(scope="module")
def config(docs):
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    return yaml.safe_load(cm["data"]["config.yaml"])


@pytest.fixture(scope="module")
def deployments_by_group(config):
    out: dict[str, list] = {}
    for m in config["model_list"]:
        out.setdefault(m["model_name"], []).append(m)
    return out


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
def router_mod(router_src):
    """El modulo completo, con httpx stubbeado (igual que el contrato de
    session_routing)."""
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        module = types.ModuleType("session_router_affinity_test")
        exec(compile(router_src, "session_router.py", "exec"), module.__dict__)
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)
    return module


# ── 1. forma de los deployments ──────────────────────────────────────────────


def test_dos_deployments_por_grupo_y_espejo_exacto(deployments_by_group):
    for g in GROUPS:
        deps = deployments_by_group[g]
        assert len(deps) == 2, f"{g}: se esperan 2 deployments (cuenta 1 y 2), hay {len(deps)}"
        k1, k2 = deps
        for p in MIRRORED_LITELLM_PARAMS:
            assert k1["litellm_params"].get(p) == k2["litellm_params"].get(p), (g, p)
        for f in MIRRORED_MODEL_INFO:
            assert k1["model_info"].get(f) == k2["model_info"].get(f), (g, f)


def test_k1_una_key_k2_la_segunda(deployments_by_group):
    for g in GROUPS:
        k1, k2 = deployments_by_group[g]
        assert k1["litellm_params"]["api_key"] == "os.environ/DASHSCOPE_API_KEY", g
        assert k2["litellm_params"]["api_key"] == "os.environ/DASHSCOPE_API_KEY_2", g


def test_k2_inerte_hasta_activacion_order2(deployments_by_group):
    """`order: 2` = failover puro — PERO el minimo se calcula SOLO entre los
    orders DECLARADOS (medido en la v1.100.0 pineada: sin order en el k1 el
    unico order del grupo era el 2 y el reparto se iba 20/20 al -k2). Por eso
    el -k1 declara `order: 1` explicitamente."""
    for g in GROUPS:
        k1, k2 = deployments_by_group[g]
        assert k1["litellm_params"].get("order") == 1, f"{g}: k1 DEBE declarar order: 1 (el minimo solo mira orders declarados)"
        assert k2["litellm_params"].get("order") == 2, f"{g}: k2 debe nacer con order: 2"


def test_ids_de_deployments_estables_y_unicos(deployments_by_group):
    """El pin de afinidad guarda model_id: si el id lo genera litellm y cambia
    al reiniciar, cada pin muere huerfano. Ids explicitos y estables."""
    vistos = set()
    for g in GROUPS:
        k1, k2 = deployments_by_group[g]
        assert k1["model_info"]["id"] == f"{g}-k1", g
        assert k2["model_info"]["id"] == f"{g}-k2", g
        vistos |= {k1["model_info"]["id"], k2["model_info"]["id"]}
    assert len(vistos) == 2 * len(GROUPS)


def test_la_key_2_solo_aparece_en_los_k2(config):
    for m in config["model_list"]:
        key = m["litellm_params"].get("api_key")
        if key == "os.environ/DASHSCOPE_API_KEY_2":
            assert m["model_info"]["id"].endswith("-k2"), m["model_info"]["id"]


# ── 2. router_settings ───────────────────────────────────────────────────────


def test_affinity_config_solo_para_alibaba_y_completa(config):
    rs = config["router_settings"]
    mgac = rs.get("model_group_affinity_config")
    assert mgac == {g: ["session_affinity"] for g in GROUPS}, (
        "session_affinity debe estar activado exactamente en los grupos alibaba-*"
    )
    assert rs.get("deployment_affinity_ttl_seconds") == 3600


# ── 3. comportamiento del stamp ──────────────────────────────────────────────


def _data(model, sid=None, metadata=None):
    data = {"model": model, "messages": [{"role": "user", "content": "hola"}]}
    if sid:
        data["headers"] = {"x-claude-code-session-id": sid}
    if metadata is not None:
        data["metadata"] = metadata
    return data


def test_estampa_sid_propio_en_alibaba(router_mod):
    router_mod.ensure_affinity_redis = lambda: None
    data = _data("alibaba-q38-flash", sid="sesion-abc")
    assert router_mod.stamp_alibaba_session_affinity(data) == "sesion-abc"
    assert data["metadata"]["session_id"] == "sesion-abc"


def test_pisa_el_session_id_del_cliente(router_mod):
    """El que manda el cliente no es estable (opencode: 22 por conversacion).
    Se PISA con el sid del hook: ese es el punto del stamp."""
    router_mod.ensure_affinity_redis = lambda: None
    data = _data("alibaba-q38-max", sid="sesion-estable",
                 metadata={"session_id": "cliente-volante"})
    assert router_mod.stamp_alibaba_session_affinity(data) == "sesion-estable"
    assert data["metadata"]["session_id"] == "sesion-estable"


def test_no_toca_otros_modelos(router_mod):
    router_mod.ensure_affinity_redis = lambda: None
    data = _data("qwen38-flash-next", sid="sesion-abc")
    assert router_mod.stamp_alibaba_session_affinity(data) is None
    assert "session_id" not in (data.get("metadata") or {})


def test_sin_sid_no_estampa_nada(router_mod):
    router_mod.ensure_affinity_redis = lambda: None
    data = {"model": "alibaba-q38-flash"}  # sin sid, sin messages
    assert router_mod.stamp_alibaba_session_affinity(data) is None
    assert "session_id" not in (data.get("metadata") or {})


def test_red_de_estabilidad_misma_conversacion_mismo_sid(router_mod):
    """Clientes sin id de sesion: el hash del prefijo (system + primer
    mensaje + cliente) identifica la conversacion entre turnos."""
    router_mod.ensure_affinity_redis = lambda: None
    d1 = {"model": "alibaba-q38-flash", "system": "tu eres X",
          "messages": [{"role": "user", "content": "primero"},
                       {"role": "assistant", "content": "respuesta"}]}
    d2 = {"model": "alibaba-q38-flash", "system": "tu eres X",
          "messages": [{"role": "user", "content": "primero"},
                       {"role": "assistant", "content": "respuesta"},
                       {"role": "user", "content": "segundo turno"}]}
    s1 = router_mod.stamp_alibaba_session_affinity(d1)
    s2 = router_mod.stamp_alibaba_session_affinity(d2)
    assert s1 and s1.startswith("pfx-")
    assert s1 == s2, "la misma conversacion tiene que clavar la misma cuenta"


# ── 4. tier redis del DualCache ──────────────────────────────────────────────


def test_tier_redis_se_monta_una_sola_vez(router_src):
    tree = ast.parse(router_src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "ensure_affinity_redis")
    body = ast.dump(fn)
    assert "_affinity_redis_tried" in body, "guarda de una-sola-vez"
    assert "_update_redis_cache" in body, "el seam documentado de litellm"
    # El claim y la lectura del pin van EN el camino de la peticion: timeout
    # duro y corto (el default de RedisCache eran 5 s).
    assert "socket_timeout=0.5" in router_src


def test_stamp_llama_al_tier_antes_de_estampar(router_src):
    tree = ast.parse(router_src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "stamp_alibaba_session_affinity")
    llamados = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "ensure_affinity_redis" in llamados


# ── 5. cableado en strip_params ──────────────────────────────────────────────


def test_strip_llama_el_stamp_fuera_del_gate_routed(strip_src):
    """El stamp debe verse en TODA peticion `alibaba-*`, incluida la pedida
    explicitamente, que no entra en ROUTED_MODELS ni pasa por
    apply_session_routing."""
    assert "session_router.stamp_alibaba_session_affinity(data)" in strip_src
    # Fuera del gate: la llamada NO puede estar dentro del `if` de
    # ROUTED_MODELS — se verifica que tras la llamada a apply_session_routing
    # hay un bloque propio que la invoca.
    idx_apply = strip_src.index("session_rerouted = await session_router.apply_session_routing")
    idx_stamp = strip_src.index("stamp_alibaba_session_affinity(data)", idx_apply)
    entre = strip_src[idx_apply:idx_stamp]
    assert "if session_router is not None:" in entre, "la llamada debe ser incondicional"


# ── 6. plomada de secretos ───────────────────────────────────────────────────


def test_externalsecret_propio_y_env_optional(docs):
    es = next(
        d for d in docs
        if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == "litellm-alibaba-2"
    )
    data = es["spec"]["data"]
    assert data == [{"secretKey": "DASHSCOPE_API_KEY_2",
                     "remoteRef": {"key": "alibaba-model-studio-2/password"}}], \
        "recurso propio y aislado: un campo que falte no puede romper la key 1"
    dep = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm")
    envs = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    ref = envs["DASHSCOPE_API_KEY_2"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "litellm-alibaba-2", "key": "DASHSCOPE_API_KEY_2", "optional": True}, \
        "sin Secret el env no existe y el -k2 queda inerte/401, nunca toca la key 1"
