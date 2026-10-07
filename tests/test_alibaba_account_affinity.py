"""Afinidad por CUENTA de Alibaba (28-09-2026): una sesion vive en UNA cuenta, en
todos los grupos `alibaba-*`, y no se mezcla.

Unitarios de `session_router.alibaba_account_filter` contra una Valkey de pega
(misma semantica que redis.asyncio para GET / SET NX EX / EXPIRE). Cada «pod» es
una copia independiente del modulo: el estado en memoria no se comparte, la
Valkey si — igual que las dos replicas del proxy.

El test de integracion con el Router REAL de litellm v1.100.0 y una Valkey real
esta en tests/integration/alibaba_account_affinity_router.py.

TTL del pin (DGX-592, 05-10-2026): INACTIVIDAD al ritmo del vinculo de plan a
Alibaba, no el ciclo del plan. Una sesion viva no muda (el keepalive renueva); una
que vuelve tras el hueco se re-sortea con los pesos del cupo, y la memoria local del
pod caduca igual — reactivar desde ella un pin caducado anularia el reparto.
"""
import asyncio
import json
import random
import sys
import time
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


def test_el_pin_se_guarda_con_su_clave_y_ttl_de_inactividad():
    """DGX-592: el pin vive lo que vive el prefijo cacheado (el tiempo del vinculo
    de plan a Alibaba), no el ciclo del plan. Con 30 dias la sesion nacida en k1 se
    quedaba en k1 defendiendo una caché muerta y el reparto por cupo no reasignaba
    nada (medido: pesos 0,28/0,72 y 51/49 del trafico real en contra)."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    run(pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("ses-1")))
    key = f"alibaba_account_pin:v1:{KEY_A}:ses-1"
    assert valkey.data[key] in ("k1", "k2")
    assert pod.ALIBABA_ACCOUNT_PIN_TTL_SECONDS == pod.STICKY_TTL_ALIBABA_SECONDS
    assert valkey.ttl[key] == pod.ALIBABA_ACCOUNT_PIN_TTL_SECONDS < 30 * 86400


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

    pod._pin_epoch = lambda now=None: 0   # con la epoca real cruzaria la ventana (DGX-639)
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


def test_sin_sid_sortea_una_cuenta_sin_escribir_pin():
    """DGX-619 (06-10-2026): antes pasaba intacta y el simple-shuffle del Router
    repartia 50/50 a ciegas, gastando la cuenta sin cupo (k1 al 91,5 %). Ahora el
    filtro sortea con `draw_account` sobre las cuentas sanas y NO escribe pin ni
    toca Valkey: no hay sesion que clavar."""
    valkey = FakeValkey()
    pod = _con_pesos(_pod(valkey), {"k1": 0.0, "k2": 1.0})
    out = run(pod.alibaba_account_filter(
        "alibaba-q38-flash", _deps("alibaba-q38-flash"),
        {"metadata": {"user_api_key_hash": KEY_A}}))
    assert _account(out) == "k2"
    assert not valkey.ops
    assert not pod._account_pins_local


def test_sin_sid_falla_abierto_a_la_lista_completa():
    """Cualquier excepcion del sorteo deja la lista intacta (fail-open, como el
    resto del modulo): una peticion anonima nunca se cae por el selector."""
    pod = _pod(FakeValkey())

    async def boom(accounts):
        raise RuntimeError("cupo ilegible")

    pod.draw_account = boom
    deps = _deps("alibaba-q38-max")
    out = run(pod.alibaba_account_filter(
        "alibaba-q38-max", deps, {"metadata": {"user_api_key_hash": KEY_A}}))
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


# ── reparto ponderado por cupo (29-09-2026) ──────────────────────────────────


def _con_pesos(pod, weights):
    async def cfg():
        return {"alibaba_account_weights": weights}
    pod._config = cfg
    return pod


def test_sesiones_nuevas_siguen_el_peso_de_cada_cuenta():
    pod = _con_pesos(_pod(FakeValkey()), {"k1": 0.25, "k2": 0.75})

    async def go():
        cuentas = []
        for i in range(4000):
            out = await pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(f"w{i}"))
            cuentas.append(_account(out))
        return cuentas

    cuentas = run(go())
    assert 0.21 < cuentas.count("k1") / len(cuentas) < 0.29
    assert pod.ACCOUNT_AFFINITY_STATS["weighted"] == 4000


def test_con_pesos_sigue_siendo_determinista_entre_pods():
    a = _con_pesos(_pod(None), {"k1": 0.3, "k2": 0.7})
    b = _con_pesos(_pod(None), {"k1": 0.3, "k2": 0.7})

    async def go():
        for i in range(300):
            ra = await a.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(f"d{i}"))
            rb = await b.alibaba_account_filter("alibaba-q38-max", _deps("alibaba-q38-max"), _kwargs(f"d{i}"))
            assert _account(ra) == _account(rb)

    run(go())


def test_el_peso_no_mueve_una_sesion_ya_clavada():
    valkey = FakeValkey()
    pod = _pod(valkey)

    async def go():
        out = await pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("fija"))
        antes = _account(out)
        otra = "k2" if antes == "k1" else "k1"
        _con_pesos(pod, {antes: 0.0, otra: 1.0})
        out = await pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("fija"))
        return antes, _account(out)

    antes, despues = run(go())
    assert antes == despues


def test_pin_caducado_con_valkey_sana_se_re_sortea_no_se_reactiva_local():
    """Un GET CORRECTO que devuelve None es un pin caducado, no una lectura fallida.
    Si la memoria del pod lo reactiva, el TTL corto no sirve de nada: la sesion se
    queda en su cuenta de nacimiento dentro de este pod para siempre (DGX-592)."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    sid = "ses-caduca"
    antes = _account(run(pod.alibaba_account_filter(
        "alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid))))
    key = pod.account_pin_key(KEY_A, sid)
    assert key in valkey.data and pod._local_pin(key) == antes

    del valkey.data[key]                     # durmio mas que el TTL: la clave ya no esta
    otra = "k2" if antes == "k1" else "k1"
    _con_pesos(pod, {antes: 0.0, otra: 1.0})  # y al volver el cupo esta del revés
    out = run(pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid)))
    assert _account(out) == otra, "el pin caducado se reactivó desde la memoria local"
    assert valkey.data[key] == otra          # el re-sorteo queda escrito


def test_la_memoria_local_caduca_como_el_pin():
    """Con Valkey caída la memoria local es la red de estabilidad, pero caduca al
    mismo tiempo que el pin: un recuerdo viejo no puede clavar la sesion."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    sid = "ses-local"
    antes = _account(run(pod.alibaba_account_filter(
        "alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid))))
    key = pod.account_pin_key(KEY_A, sid)
    pod._account_pins_local[key] = (antes, time.monotonic() - 1)   # recuerdo caducado
    del valkey.data[key]
    otra = "k2" if antes == "k1" else "k1"
    _con_pesos(pod, {antes: 0.0, otra: 1.0})
    valkey.down = True
    out = run(pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid)))
    assert _account(out) == otra


def test_cuenta_con_peso_cero_no_recibe_sesiones_nuevas():
    pod = _con_pesos(_pod(None), {"k1": 0.0, "k2": 1.0})

    async def go():
        for i in range(200):
            out = await pod.alibaba_account_filter("alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(f"z{i}"))
            assert _account(out) == "k2"

    run(go())


@pytest.mark.parametrize("raw,esperado", [
    ({"k1": 0.4, "k2": 0.6}, {"k1": 0.4, "k2": 0.6}),
    ({"k1": 0, "k2": 0}, {}),
    ({"k1": -1, "k2": 1}, {}),
    ({"k1": True, "k2": 1}, {}),
    ({"k1": float("nan"), "k2": 1}, {}),
    ({"x": 1, "k2": 1}, {"k2": 1.0}),
    ("k1", {}),
    (None, {}),
])
def test_sanitize_de_los_pesos(raw, esperado):
    pod = _pod(None)
    assert pod._sanitize({"alibaba_account_weights": raw})["alibaba_account_weights"] == esperado


def test_pesos_que_no_cubren_las_cuentas_caen_al_uniforme():
    pod = _pod(None)
    assert pod.preferred_account("u", "s", ["k1", "k2"], {"k2": 1.0}) == pod.preferred_account("u", "s", ["k1", "k2"])


# ── seam publico del selector: peticiones SIN sesion (DGX-619, 06-10-2026) ───

PESOS_DGX619 = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "alibaba_account_weights_dgx619.json").read_text())


def _sorteos(pod, n):
    async def go():
        return [await pod.draw_account(["k1", "k2"]) for _ in range(n)]
    return run(go())


def test_sin_sid_draw_account_real_reparte_por_los_pesos_del_fixture():
    """C3 (DGX-619): 10 000 sorteos por el `draw_account` REAL (no mock) con los
    pesos fijos del fixture {k1: 0.17, k2: 0.83} => k2 entre 73 % y 93 %
    (sigma ~0,4 pt: no flaquea). Es la unica formula que veran el chat anonimo y
    el gateway de media (DGX-621): el seam, no un mock del selector."""
    assert PESOS_DGX619 == {"k1": 0.17, "k2": 0.83}
    pod = _con_pesos(_pod(None), PESOS_DGX619)
    draws = _sorteos(pod, 10000)
    assert len(draws) == 10000 and set(draws) == {"k1", "k2"}
    p2 = draws.count("k2") / len(draws)
    assert 0.73 < p2 < 0.93, p2


def test_sin_sid_sin_pesos_config_fria_salen_tramos_iguales():
    """Regresión del rojo del 29-09 (run 36583219046) aplicada al sorteo anonimo:
    con la caché de config FRIA (sin pesos) no puede haber una segunda formula —
    `draw_account` usa los mismos tramos con peso 1, asi que el reparto sale
    uniforme y el frio/caliente no son dos lecturas del hash."""
    frio = _pod(None)  # _account_weights() sobre DEFAULT_CONFIG => {}
    draws = _sorteos(frio, 4000)
    assert 0.41 < draws.count("k2") / len(draws) < 0.59, draws.count("k2")


def test_warm_config_refresca_sin_lanzar_nunca():
    """Lo llama el lifespan del gateway (DGX-621) antes de aceptar trafico, para
    no repartir en frio: renueva la TTL de la caché incluso con el panel
    inalcanzable (httpx stubbeado) y nunca propaga la excepcion."""
    pod = _pod(None)
    run(pod.warm_config())
    assert pod._config_cache["expires"] > time.monotonic()


def test_pesos_uniformes_y_cachefria_eligen_la_misma_cuenta():
    """Regresión del rojo de CI del 29-09 (run 36583219046): el reparto con pesos
    leía el hash por tramos acumulados y el uniforme por `h % len` — dos lecturas
    distintas del mismo hash. Un pod con la caché de config en frío ({}) y otro ya
    con los pesos proyectados elegían cuentas DISTINTAS para la misma sesión nueva:
    sesiones mezcladas. Con pesos = 1 (o sin pesos) la elección tiene que ser
    idéntica, y con pesos sesgados tiene que seguir siendo determinista."""
    pod = _pod(None)
    for i in range(500):
        sid = f"cold{i}"
        sin_pesos = pod.preferred_account(KEY_A, sid, ["k1", "k2"])
        uniformes = pod.preferred_account(KEY_A, sid, ["k1", "k2"], {"k1": 1.0, "k2": 1.0})
        assert sin_pesos == uniformes, (sid, sin_pesos, uniformes)
    sesgados = [pod.preferred_account(KEY_A, f"w{i}", ["k1", "k2"], {"k1": 0.45, "k2": 0.55})
                for i in range(4000)]
    assert 0.41 < sesgados.count("k2") / len(sesgados) < 0.59


# ── re-sorteo independiente tras caducar el pin (DGX-639) ────────────────────
# Sesgo medido el 06-10: el hash(api-key-hash, sid) daba SIEMPRE el mismo punto, asi
# que una sesion pesada con sid estable (hermes-batch) caia en la misma cuenta en
# cada re-sorteo (k1 34 % del gasto con peso 0,16). La epoca de ventana = TTL del
# pin entra en el hash: otra ventana, otro punto.

SIDS_PESADAS = ["hermes-batch-pfx", "pfx-a1b2c3", "ses-pesada-1", "ses-pesada-2", "ses-pesada-3"]


def _epoca(pod, e):
    pod._pin_epoch = lambda now=None, e=e: e


def test_resorteo_tras_caducar_el_pin_es_independiente_entre_ventanas():
    valkey = FakeValkey()
    pod = _con_pesos(_pod(valkey), {"k1": 0.16, "k2": 0.84})

    async def go(sid):
        cuentas = []
        for e in range(600):
            _epoca(pod, e)
            # lo que hace un pin que caduco por inactividad: ni Valkey ni memoria local
            valkey.data.clear()
            valkey.ttl.clear()
            pod._account_pins_local.clear()
            out = await pod.alibaba_account_filter(
                "alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid))
            cuentas.append(_account(out))
        return cuentas

    for sid in SIDS_PESADAS:
        cuentas = run(go(sid))
        fraccion = cuentas.count("k1") / len(cuentas)
        assert abs(fraccion - 0.16) <= 0.06, (sid, fraccion)
        assert len(set(cuentas)) == 2, (sid, set(cuentas))


def test_la_epoca_nace_de_el_ttl_del_pin():
    pod = _pod(None)
    pod.ALIBABA_ACCOUNT_PIN_TTL_SECONDS = 600
    assert pod._pin_epoch(0) == 0
    assert pod._pin_epoch(599.9) == 0
    assert pod._pin_epoch(600) == 1
    rnd = random.Random(7)
    for _ in range(1000):
        t_nac = rnd.uniform(0, 10 ** 9)
        # un pin caduca por inactividad: el re-sorteo llega tras nacimiento + TTL
        assert pod._pin_epoch(t_nac + pod.ALIBABA_ACCOUNT_PIN_TTL_SECONDS + 1) != pod._pin_epoch(t_nac)
    pod.ALIBABA_ACCOUNT_PIN_TTL_SECONDS = 0   # sin division por cero
    assert pod._pin_epoch(5) == 5


def test_pin_vivo_no_se_re_sortea_aunque_cambie_la_epoca():
    valkey = FakeValkey()
    pod = _con_pesos(_pod(valkey), {"k1": 0.5, "k2": 0.5})
    _epoca(pod, 0)
    antes = _account(run(pod.alibaba_account_filter(
        "alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("viva"))))
    claims = pod.ACCOUNT_AFFINITY_STATS["claims"]
    for e in (1, 5, 100):
        _epoca(pod, e)
        out = run(pod.alibaba_account_filter(
            "alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs("viva")))
        assert _account(out) == antes
    assert pod.ACCOUNT_AFFINITY_STATS["claims"] == claims


def test_dos_pods_sin_valkey_misma_epoca_misma_cuenta():
    a = _con_pesos(_pod(None), {"k1": 0.3, "k2": 0.7})
    b = _con_pesos(_pod(None), {"k1": 0.3, "k2": 0.7})
    _epoca(a, 41)
    _epoca(b, 41)

    async def cuentas(pod, sids):
        return [_account(await pod.alibaba_account_filter(
            "alibaba-q38-flash", _deps("alibaba-q38-flash"), _kwargs(sid))) for sid in sids]

    sids = [f"rep{i}" for i in range(200)]
    en_a = run(cuentas(a, sids))
    assert en_a == run(cuentas(b, sids))
    # no es trivial: otra epoca en otro pod mueve al menos un sid
    c = _con_pesos(_pod(None), {"k1": 0.3, "k2": 0.7})
    _epoca(c, 42)
    assert en_a != run(cuentas(c, sids))
