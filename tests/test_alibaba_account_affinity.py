"""Afinidad por CUENTA de Alibaba (28-09-2026): una sesion vive en UNA cuenta, en
todos los grupos `alibaba-*`, y no se mezcla.

Unitarios de `session_router.alibaba_account_filter` contra una Valkey de pega
(misma semantica que redis.asyncio para GET / SET NX EX / EXPIRE). Cada «pod» es
una copia independiente del modulo: el estado en memoria no se comparte, la
Valkey si — igual que las dos replicas del proxy.

El test de integracion con el Router REAL de litellm v1.100.0 y una Valkey real
esta en tests/integration/alibaba_account_affinity_router.py.
"""
import asyncio
import random
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
KEY_A = "a" * 64
KEY_B = "b" * 64


def _configmap():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    return next(d for d in docs
                if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")


ROUTER_SRC = _configmap()["data"]["session_router.py"]
STRIP_SRC = _configmap()["data"]["litellm_strip_params.py"]


class FakeValkey:
    """GET / SET(nx, ex) / EXPIRE con TTL en segundos de un reloj propio."""

    def __init__(self):
        self.data = {}
        self.ttl = {}
        self.down = False
        self.ops = []

    def _check(self):
        if self.down:
            raise ConnectionError("valkey caida")

    async def get(self, key):
        self._check()
        self.ops.append(("get", key))
        return self.data.get(key)

    async def set(self, key, value, nx=False, ex=None):
        self._check()
        self.ops.append(("set", key, value, nx))
        if nx and key in self.data:
            return None
        self.data[key] = value
        self.ttl[key] = ex
        return True

    async def expire(self, key, seconds):
        self._check()
        self.ops.append(("expire", key))
        if key in self.data:
            self.ttl[key] = seconds
            return True
        return False


def _pod(valkey):
    """Una replica: modulo propio (memoria propia) y la Valkey compartida."""
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        mod = types.ModuleType("session_router_pod")
        exec(compile(ROUTER_SRC, "session_router.py", "exec"), mod.__dict__)
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)

    async def fake_redis():
        return None if valkey is None else valkey

    mod._redis = fake_redis
    return mod


def _deps(group, accounts=("k1", "k2")):
    return [{"model_name": group, "model_info": {"id": f"{group}-{a}"},
             "litellm_params": {"model": "openai/x"}} for a in accounts]


def _kwargs(sid, key=KEY_A):
    return {"metadata": {"session_id": sid, "user_api_key_hash": key}}


def _account(deps):
    accounts = {d["model_info"]["id"].rsplit("-", 1)[1] for d in deps}
    assert len(accounts) == 1, f"el filtro devolvio varias cuentas: {accounts}"
    return accounts.pop()


async def _drain():
    # deja correr los keepalive en background
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


# ── la regla: una sesion, una cuenta, en todos los grupos ────────────────────


def test_una_sesion_misma_cuenta_en_los_diez_grupos_y_entre_pods():
    valkey = FakeValkey()
    pods = [_pod(valkey), _pod(valkey)]
    rnd = random.Random(7)

    async def go():
        seen = {}
        for turn in range(2000):
            sid = f"ses-{rnd.randrange(300)}"
            group = rnd.choice(GROUPS)
            pod = rnd.choice(pods)
            out = await pod.alibaba_account_filter(group, _deps(group), _kwargs(sid))
            acc = _account(out)
            assert seen.setdefault(sid, acc) == acc, f"{sid} mezclo cuentas en {group}"
        await _drain()
        return seen

    seen = run(go())
    counts = {a: list(seen.values()).count(a) for a in ("k1", "k2")}
    # reparto sano: ninguna cuenta se queda con todo
    assert 0.35 < counts["k1"] / len(seen) < 0.65, counts


def test_cuenta_nueva_es_la_misma_en_dos_pods_sin_valkey():
    """Sin Valkey (caida o sin montar) la eleccion es determinista: dos replicas
    eligen la misma cuenta para la misma sesion sin hablar entre si."""
    a, b = _pod(None), _pod(None)

    async def go():
        for i in range(200):
            sid = f"s{i}"
            ra = await a.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid))
            rb = await b.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max"), _kwargs(sid))
            assert _account(ra) == _account(rb)

    run(go())


def test_el_pin_se_guarda_con_su_clave_y_ttl_de_30_dias():
    valkey = FakeValkey()
    pod = _pod(valkey)
    run(pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("ses-1")))
    key = f"alibaba_account_pin:v1:{KEY_A}:ses-1"
    assert valkey.data[key] in ("k1", "k2")
    assert valkey.ttl[key] == 30 * 86400 == pod.ALIBABA_ACCOUNT_PIN_TTL_SECONDS


def test_el_pin_guardado_manda_sobre_la_eleccion_determinista():
    valkey = FakeValkey()
    pod = _pod(valkey)
    sid = next(f"s{i}" for i in range(100)
               if pod.preferred_account(KEY_A, f"s{i}", {"k1": 1, "k2": 1}) == "k1")
    valkey.data[pod.account_pin_key(KEY_A, sid)] = "k2"
    for g in GROUPS:
        out = run(pod.alibaba_account_filter(g, _deps(g), _kwargs(sid)))
        assert _account(out) == "k2"


def test_keepalive_renueva_el_ttl_en_cada_acierto():
    valkey = FakeValkey()
    pod = _pod(valkey)
    key = pod.account_pin_key(KEY_A, "ses-k")
    valkey.data[key] = "k1"
    valkey.ttl[key] = 5

    async def go():
        await pod.alibaba_account_filter("alibaba-glm-53", _deps("alibaba-glm-53"), _kwargs("ses-k"))
        await _drain()

    run(go())
    assert valkey.ttl[key] == pod.ALIBABA_ACCOUNT_PIN_TTL_SECONDS


def test_carrera_entre_pods_gana_el_primero_y_los_dos_lo_respetan():
    """SET NX: si otro pod ya reclamo (p. ej. por una mudanza), el segundo no pisa."""
    valkey = FakeValkey()
    a, b = _pod(valkey), _pod(valkey)
    key = a.account_pin_key(KEY_A, "ses-r")
    real_get = valkey.get
    calls = {"n": 0}

    async def get_then_someone_claims(k):
        # el pod B lee «no hay pin», y justo entonces A escribe k2
        v = await real_get(k)
        calls["n"] += 1
        if calls["n"] == 1 and k == key:
            valkey.data[key] = "k2"
        return v

    valkey.get = get_then_someone_claims
    out = run(b.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max"), _kwargs("ses-r")))
    assert _account(out) == "k2" == valkey.data[key]


def test_misma_sesion_con_otra_api_key_es_otra_sesion():
    valkey = FakeValkey()
    pod = _pod(valkey)
    run(pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("s", KEY_A)))
    run(pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("s", KEY_B)))
    assert pod.account_pin_key(KEY_A, "s") in valkey.data
    assert pod.account_pin_key(KEY_B, "s") in valkey.data


# ── mudanza: la unica forma de cambiar de cuenta, y sin ida y vuelta ──────────


def test_cuenta_en_cooldown_muda_la_sesion_entera_y_no_vuelve():
    valkey = FakeValkey()
    a, b = _pod(valkey), _pod(valkey)
    key = a.account_pin_key(KEY_A, "ses-m")
    valkey.data[key] = "k1"

    async def go():
        # k1 de q38-max en cooldown: el Router solo pasa el -k2 como sano
        out = await a.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max", ("k2",)), _kwargs("ses-m"))
        assert _account(out) == "k2"
        assert valkey.data[key] == "k2", "la mudanza reescribe el pin"
        # k1 vuelve a estar sano: la sesion SIGUE en k2, en ese grupo y en todos, en los dos pods
        for pod in (a, b):
            for g in GROUPS:
                out = await pod.alibaba_account_filter(g, _deps(g), _kwargs("ses-m"))
                assert _account(out) == "k2", f"volvio a k1 en {g}"

    run(go())
    assert a.ACCOUNT_AFFINITY_STATS["moves"] == 1


def test_pin_basura_se_trata_como_mudanza():
    valkey = FakeValkey()
    pod = _pod(valkey)
    key = pod.account_pin_key(KEY_A, "ses-x")
    valkey.data[key] = "k9"
    out = run(pod.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max"), _kwargs("ses-x")))
    assert _account(out) == valkey.data[key]
    assert valkey.data[key] in ("k1", "k2")


def test_sesion_nueva_con_una_cuenta_en_cooldown_nace_en_la_sana():
    valkey = FakeValkey()
    pod = _pod(valkey)
    sid = next(f"s{i}" for i in range(100)
               if pod.preferred_account(KEY_A, f"s{i}", {"k1": 1, "k2": 1}) == "k1")
    out = run(pod.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max", ("k2",)), _kwargs(sid)))
    assert _account(out) == "k2" == valkey.data[pod.account_pin_key(KEY_A, sid)]


# ── siembra desde el pin nativo por grupo (sesiones anteriores al 28-09) ─────


def test_sesion_con_pin_nativo_conserva_su_cuenta():
    valkey = FakeValkey()
    pod = _pod(valkey)
    for want in ("k1", "k2"):
        sid = next(f"n{want}{i}" for i in range(200)
                   if pod.preferred_account(KEY_A, f"n{want}{i}", {"k1": 1, "k2": 1}) != want)
        native = f"deployment_affinity:v1:session:alibaba-q38-flash:{KEY_A}:{sid}"
        valkey.data[native] = '{"model_id": "alibaba-q38-flash-%s"}' % want
        out = run(pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid)))
        assert _account(out) == want, "el cambio de mecanismo no puede mover una sesion viva"
        assert valkey.data[pod.account_pin_key(KEY_A, sid)] == want
    assert pod.ACCOUNT_AFFINITY_STATS["seeded"] == 2


# ── Valkey caida: no mezcla lo que el pod ya conoce ni falla la peticion ──────


def test_valkey_caida_mantiene_la_cuenta_conocida_incluida_una_mudanza():
    valkey = FakeValkey()
    pod = _pod(valkey)
    key = pod.account_pin_key(KEY_A, "ses-d")
    valkey.data[key] = "k1"

    async def go():
        await pod.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max", ("k2",)), _kwargs("ses-d"))
        valkey.down = True
        for g in GROUPS:
            out = await pod.alibaba_account_filter(g, _deps(g), _kwargs("ses-d"))
            assert _account(out) == "k2"

    run(go())
    assert pod.ACCOUNT_AFFINITY_STATS["redis_errors"] >= len(GROUPS)


def test_valkey_lenta_cuenta_como_caida_y_no_estira_la_peticion():
    valkey = FakeValkey()
    pod = _pod(valkey)

    async def slow_get(key):
        await asyncio.sleep(5)

    valkey.get = slow_get

    async def go():
        t0 = asyncio.get_running_loop().time()
        out = await pod.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max"), _kwargs("ses-s"))
        return out, asyncio.get_running_loop().time() - t0

    out, dt = run(go())
    assert _account(out) == pod.preferred_account(KEY_A, "ses-s", {"k1": 1, "k2": 1})
    assert dt < 1.0


def test_memoria_local_acotada():
    pod = _pod(None)
    pod._ACCOUNT_PINS_LOCAL_MAX = 50

    async def go():
        for i in range(500):
            await pod.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max"), _kwargs(f"s{i}"))

    run(go())
    assert len(pod._account_pins_local) <= 50


# ── lo que NO toca ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model,deps", [
    ("qwen38-flash-next", _deps("qwen38-flash-next")),
    ("alibaba-q38-max", [{"model_info": {"id": "sin-sufijo"}}]),
    ("alibaba-q38-max", []),
])
def test_passthrough_fuera_de_alibaba_o_sin_ids_de_cuenta(model, deps):
    valkey = FakeValkey()
    pod = _pod(valkey)
    out = run(pod.alibaba_account_filter(model, deps, _kwargs("ses")))
    assert out is deps
    assert not valkey.ops


def test_sin_sid_no_se_filtra():
    valkey = FakeValkey()
    pod = _pod(valkey)
    deps = _deps("alibaba-q38-max")
    out = run(pod.alibaba_account_filter("alibaba-q38-max", deps, {"metadata": {"user_api_key_hash": KEY_A}}))
    assert out is deps


def test_el_sid_se_lee_tambien_de_litellm_metadata():
    valkey = FakeValkey()
    pod = _pod(valkey)
    kw = {"litellm_metadata": {"session_id": "ses-lm", "user_api_key_hash": KEY_A}}
    run(pod.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max"), kw))
    assert pod.account_pin_key(KEY_A, "ses-lm") in valkey.data


# ── cableado ──────────────────────────────────────────────────────────────────


def test_el_callback_existente_delega_en_el_filtro_y_falla_abierto():
    """El Router llama async_filter_deployments de los callbacks de litellm; el
    que ya existe (StripUnsupportedParams) delega. session_router sigue sin ser
    callback (mandato 2)."""
    i = STRIP_SRC.index("class StripUnsupportedParams")
    j = STRIP_SRC.index("    async def async_pre_call_hook", i)
    body = STRIP_SRC[i:j]
    assert "async def async_filter_deployments(" in body
    assert "session_router.alibaba_account_filter(" in body
    assert "except Exception" in body and "return healthy_deployments" in body
    assert "CustomLogger" not in ROUTER_SRC


def test_el_nativo_por_grupo_no_esta_activado_a_la_vez():
    cfg = yaml.safe_load(_configmap()["data"]["config.yaml"])
    rs = cfg["router_settings"]
    assert "model_group_affinity_config" not in rs
    assert "session_affinity" not in (rs.get("optional_pre_call_checks") or [])
