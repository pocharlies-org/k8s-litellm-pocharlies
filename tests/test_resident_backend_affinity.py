"""DGX-852 (14-10-2026): afinidad por BACKEND del residente — una conversacion, un motor.

Desde el 2+1 el residente `qwen38-flash-next` son DOS vLLM que sirven el MISMO
modelo: el par TP=2 (dgx2+dgx1, ~3,2M de tokens KV y 16 secuencias) y un TP=1
(dgx3, ~1,0M y 8). Sus cachés de prefijo NO se comparten, así que un turno que
salta de motor re-prefría el contexto entero (25-30 s medidos a 64K). Criterio de
Dani: un solo pool, cada conversacion clavada a un backend y las nuevas repartidas
por capacidad.

Unitarios de `session_router.resident_backend_filter` con la MISMA valkey de pega
que la afinidad de cuenta (importada de ahí, no copiada): GET / SET(nx, ex) /
EXPIRE sobre un reloj propio, y un módulo por «pod» — memoria separada, Valkey
compartida, como las dos réplicas del proxy.

Lo que NO se prueba aqui (porque no cabe en un unitario): que el Router no aparque
el deployment. Eso lo fija el ancla `&resident_never_cools` (cooldown 0 y politica de
fallos) y lo mide tests/integration/resident_twin_cooldown.py contra el Router real.
"""
import random

import pytest
import yaml

from test_alibaba_account_affinity import (  # noqa: F401  (harness compartido a proposito)
    KEY_A,
    KEY_B,
    MANIFEST,
    STRIP_SRC,
    FakeValkey,
    _deps,
    _drain,
    _kwargs,
    _pod,
    run,
)

FAMILIA = ("qwen38-flash-next", "qwen38-flash-next-uncensored")
GEM = ("tp2", "tp1")


def _backend(deps):
    """El sufijo `-tpN` comun a las deployments que deja el filtro."""
    backends = {d["model_info"]["id"].rsplit("-", 1)[1] for d in deps}
    assert len(backends) == 1, f"el filtro devolvio varios backends: {backends}"
    return backends.pop()


def _gemelos(group):
    return _deps(group, GEM)


def _config():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if (doc and doc.get("kind") == "ConfigMap"
                and doc["metadata"]["name"] == "litellm-config"):
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


# ── la regla: una conversacion, un backend, en toda la familia ────────────────


def test_una_conversacion_un_backend_en_toda_la_familia_y_entre_pods():
    valkey = FakeValkey()
    pods = [_pod(valkey), _pod(valkey)]
    rnd = random.Random(11)

    async def go():
        seen = {}
        for _ in range(2000):
            sid = f"ses-{rnd.randrange(300)}"
            alias = rnd.choice(FAMILIA)
            pod = rnd.choice(pods)
            out = await pod.resident_backend_filter(alias, _gemelos(alias), _kwargs(sid))
            b = _backend(out)
            assert seen.setdefault(sid, b) == b, f"{sid} mezclo backends en {alias}"
        await _drain()
        return seen

    seen = run(go())
    share = list(seen.values()).count("tp2") / len(seen)
    # reparto 3:1 (DGX-950), con la deriva de un sorteo por ventana
    assert 0.64 < share < 0.86, f"el reparto 3:1 salio {share:.2f} de tp2"


def test_sin_valkey_los_dos_pods_eligen_el_mismo_backend():
    """Sin Valkey (caida o sin montar) la eleccion es determinista: dos replicas
    clavan la misma sesion al mismo motor sin hablar entre si."""
    a, b = _pod(None), _pod(None)

    async def go():
        for i in range(200):
            sid = f"s{i}"
            ra = await a.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs(sid))
            rb = await b.resident_backend_filter(FAMILIA[1], _gemelos(FAMILIA[1]), _kwargs(sid))
            assert _backend(ra) == _backend(rb)

    run(go())


def test_el_pin_lleva_su_clave_su_valor_y_su_ttl_de_inactividad():
    valkey = FakeValkey()
    pod = _pod(valkey)
    run(pod.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs("ses-1")))
    key = f"resident_backend_pin:v1:{KEY_A}:ses-1"
    assert valkey.data[key] in GEM
    assert valkey.ttl[key] == pod.RESIDENT_BACKEND_PIN_TTL_SECONDS
    # TTL de INACTIVIDAD al ritmo del vinculo local (30 min), no de un ciclo de plan.
    assert pod.RESIDENT_BACKEND_PIN_TTL_SECONDS == pod.STICKY_TTL_LOCAL_SECONDS
    assert pod.RESIDENT_BACKEND_PIN_TTL_SECONDS < 3600
    # y es OTRA familia de claves: no se cruza nunca con el pin de cuenta
    assert not any(k.startswith("alibaba_account_pin") for k in valkey.data)


def test_el_pin_leyido_manda_sobre_el_sorteo_ponderado():
    valkey = FakeValkey()
    pod = _pod(valkey)
    sid = next(f"s{i}" for i in range(200)
               if pod.preferred_account(KEY_A, f"s{i}", dict.fromkeys(GEM),
                                        {"tp2": 3, "tp1": 1}) == "tp2")
    valkey.data[pod.backend_pin_key(KEY_A, sid)] = "tp1"
    for alias in FAMILIA:
        out = run(pod.resident_backend_filter(alias, _gemelos(alias), _kwargs(sid)))
        assert _backend(out) == "tp1"


def test_keepalive_renueva_el_ttl_en_cada_acierto():
    valkey = FakeValkey()
    pod = _pod(valkey)
    key = pod.backend_pin_key(KEY_A, "ses-k")
    valkey.data[key] = "tp2"
    valkey.ttl[key] = 5

    async def go():
        await pod.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs("ses-k"))
        await _drain()

    run(go())
    assert valkey.ttl[key] == pod.RESIDENT_BACKEND_PIN_TTL_SECONDS


def test_misma_sesion_con_otra_api_key_es_otra_sesion():
    valkey = FakeValkey()
    pod = _pod(valkey)
    run(pod.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs("s", KEY_A)))
    run(pod.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs("s", KEY_B)))
    assert pod.backend_pin_key(KEY_A, "s") in valkey.data
    assert pod.backend_pin_key(KEY_B, "s") in valkey.data


# ── mudanza: la unica forma de cambiar de motor, y sin ida y vuelta ───────────


def test_backend_en_cooldown_muda_la_sesion_entera_y_no_vuelve():
    """Es la rama que hace falta con dos motores: el pin NO puede atar una
    conversacion a un deployment que el Router ya aparcó."""
    valkey = FakeValkey()
    a, b = _pod(valkey), _pod(valkey)
    key = a.backend_pin_key(KEY_A, "ses-m")
    valkey.data[key] = "tp2"

    async def go():
        out = await a.resident_backend_filter(
            FAMILIA[0], _deps(FAMILIA[0], ("tp1",)), _kwargs("ses-m"))
        assert _backend(out) == "tp1"
        assert valkey.data[key] == "tp1", "la mudanza reescribe el pin"
        for pod in (a, b):
            for alias in FAMILIA:
                out = await pod.resident_backend_filter(alias, _gemelos(alias), _kwargs("ses-m"))
                assert _backend(out) == "tp1", f"volvio a tp2 en {alias}"

    run(go())
    assert a.BACKEND_AFFINITY_STATS["moves"] == 1


def test_pin_basura_se_trata_como_mudanza():
    valkey = FakeValkey()
    pod = _pod(valkey)
    key = pod.backend_pin_key(KEY_A, "ses-x")
    valkey.data[key] = "tp9"
    out = run(pod.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs("ses-x")))
    assert _backend(out) in GEM
    assert valkey.data[key] in GEM


def test_sesion_nueva_con_un_motor_en_cooldown_nace_en_el_sano_sin_pin():
    """Un solo backend sano y sin pin: no hay nada que elegir, y escribir un pin
    que no eligio nadie ataria la sesion a donde caia por casualidad. Pasa intacta
    y NO se escribe."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    deps = _deps(FAMILIA[0], ("tp1",))
    out = run(pod.resident_backend_filter(FAMILIA[0], deps, _kwargs("ses-nueva")))
    assert out is deps
    assert not valkey.data


def test_sesion_que_nace_durante_la_caida_vuelve_a_sortearse_al_volver_el_gemelo():
    valkey = FakeValkey()
    pod = _pod(valkey)
    run(pod.resident_backend_filter(FAMILIA[0], _deps(FAMILIA[0], ("tp1",)), _kwargs("ses-fria")))
    assert not valkey.data, "la caida no deja ligadura"
    out = run(pod.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs("ses-fria")))
    assert _backend(out) in GEM
    assert valkey.data[pod.backend_pin_key(KEY_A, "ses-fria")] in GEM


# ── peticiones sin sesion y grupos que no son del residente ──────────────────


def test_peticion_anonima_sorteo_ponderado_y_no_escribe_pin():
    valkey = FakeValkey()
    pod = _pod(valkey)

    async def go():
        vistos = []
        for i in range(400):
            kwargs = {"metadata": {"user_api_key_hash": KEY_A}}   # sin session_id
            out = await pod.resident_backend_filter(
                FAMILIA[0], _gemelos(FAMILIA[0]), kwargs)
            assert len(out) == 1, "una peticion suelta va a UN motor, no a los dos"
            vistos.append(_backend(out))
        return vistos

    vistos = run(go())
    share = vistos.count("tp2") / len(vistos)
    assert 0.64 < share < 0.86, f"el reparto 3:1 salio {share:.2f}"
    assert not valkey.data, "sin sid no hay conversacion que clavar"


@pytest.mark.parametrize("model,deps", [
    # alias de la familia pero SIN ids `-tpN`: el manifiesto de antes del 2+1, o
    # un rollback. No hay backend que reconocer => lista intacta.
    ("qwen38-flash-next", [{"model_name": "qwen38-flash-next",
                            "model_info": {"id": "qwen38-flash-next"}}]),
    # `correo-clasificador`: fuera de la familia (una sola deployment en el par).
    ("correo-clasificador", _deps("correo-clasificador", ("tp2",))),
    # Los grupos de Alibaba los decide SU filtro, con SU ancla: nunca este.
    ("alibaba-q38-max", _deps("alibaba-q38-max")),
    ("qwen38-flash-next", []),
])
def test_passthrough_fuera_de_la_familia_o_sin_gemelos(model, deps):
    """Y `is` (la MISMA lista), no una copia: el Router compara identidad al
    montar el deployment elegido."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    out = run(pod.resident_backend_filter(model, deps, _kwargs("ses")))
    assert out is deps
    assert not valkey.data


def test_valkey_caida_mantiene_el_backend_conocido_incluida_una_mudanza():
    """Fail-open: si Valkey no contesta, el pod decide con su memoria y la
    peticion NO falla."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    key = pod.backend_pin_key(KEY_A, "ses-d")
    valkey.data[key] = "tp2"

    async def go():
        await pod.resident_backend_filter(
            FAMILIA[0], _deps(FAMILIA[0], ("tp1",)), _kwargs("ses-d"))
        valkey.down = True
        for alias in FAMILIA:
            out = await pod.resident_backend_filter(alias, _gemelos(alias), _kwargs("ses-d"))
            assert _backend(out) == "tp1"

    run(go())
    assert pod.BACKEND_AFFINITY_STATS["redis_errors"] >= len(FAMILIA)


def test_pesos_mal_escritos_dejan_de_ser_ponderados_pero_no_rompen(monkeypatch):
    monkeypatch.setenv("SESSION_ROUTER_RESIDENT_WEIGHTS", "tp2=3,basura,tp1=no-numero")
    pod = _pod(FakeValkey())
    assert pod._resident_backend_weights() == {"tp2": 3.0}
    out = run(pod.resident_backend_filter(FAMILIA[0], _gemelos(FAMILIA[0]), _kwargs("ses-p")))
    assert _backend(out) in GEM


def test_el_default_de_pesos_es_3_a_1():
    pod = _pod(None)
    assert pod.RESIDENT_BACKEND_WEIGHTS_DEFAULT == "tp2=3,tp1=1"
    assert pod._resident_backend_weights() == {"tp2": 3.0, "tp1": 1.0}


# ── el sid que necesita el filtro tiene que llegar en la peticion ────────────


def _data(model, sid=None, metadata=None):
    data = {"model": model, "messages": [{"role": "user", "content": "hola"}]}
    if sid:
        data["headers"] = {"x-claude-code-session-id": sid}
    if metadata is not None:
        data["metadata"] = metadata
    return data


def test_el_stamp_estampa_sid_en_la_familia_y_no_en_otros():
    pod = _pod(None)
    for alias in FAMILIA:
        data = _data(alias, sid="sesion-abc")
        assert pod.stamp_resident_session_affinity(data) == "sesion-abc"
        assert data["metadata"]["session_id"] == "sesion-abc"
    for ajeno in ("correo-clasificador", "alibaba-q38-flash", "tooling"):
        data = _data(ajeno, sid="sesion-abc")
        assert pod.stamp_resident_session_affinity(data) is None
        assert "session_id" not in (data.get("metadata") or {})


def test_el_stamp_pisa_el_session_id_del_cliente():
    """El id que manda el cliente no es estable (opencode: 22 por conversacion).
    Se PISA con el sid del hook, igual que en Alibaba: si no, el filtro clavaria
    una conversacion distinta en cada turno."""
    pod = _pod(None)
    data = _data(FAMILIA[0], sid="sesion-estable", metadata={"session_id": "cliente-volante"})
    assert pod.stamp_resident_session_affinity(data) == "sesion-estable"
    assert data["metadata"]["session_id"] == "sesion-estable"


def test_sin_sid_estable_la_conversacion_se_reconoce_por_su_prefijo():
    pod = _pod(None)
    d1 = {"model": FAMILIA[0], "system": "tu eres X",
          "messages": [{"role": "user", "content": "primero"}]}
    d2 = {"model": FAMILIA[0], "system": "tu eres X",
          "messages": [{"role": "user", "content": "primero"},
                       {"role": "assistant", "content": "respuesta"},
                       {"role": "user", "content": "segundo turno"}]}
    s1 = pod.stamp_resident_session_affinity(d1)
    s2 = pod.stamp_resident_session_affinity(d2)
    assert s1 and s1.startswith("pfx-")
    assert s1 == s2, "la misma conversacion tiene que clavar el mismo motor"


# ── el enganche en el hook y el manifiesto ────────────────────────────────────


def test_el_hook_encadena_los_dos_filtros_y_estampa_antes_de_priority():
    llama = STRIP_SRC.index("session_router.resident_backend_filter(")
    cuenta = STRIP_SRC.index("session_router.alibaba_account_filter(")
    # El filtro de backend va DESPUES del de cuenta: el de Alibaba decide la
    # cuenta y este reparte el residente; al reves se filtraria dos veces.
    assert cuenta < llama
    assert "resident_backend_filter fallo" in STRIP_SRC, "cada filtro fail-open por separado"
    stamp = STRIP_SRC.index("session_router.stamp_resident_session_affinity(data)")
    assert stamp < STRIP_SRC.index("apply_priority", stamp), "el sid tiene que estar antes de priority"


def test_los_gemelos_del_residente_solo_difieren_en_el_motor():
    """El TP=1 es el MISMO modelo con el MISMO sello `refusal:λ`, el MISMO timeout
    y el MISMO cooldown: se heredan por ancla YAML. Un valor explicito puesto en
    un gemelo y no en el otro es exactamente como dos nombres dejan de significar
    lo mismo, y aqui se ve."""
    por_alias = {}
    for e in _config()["model_list"]:
        if e["model_name"] in FAMILIA:
            por_alias.setdefault(e["model_name"], []).append(e)
    for alias, entradas in por_alias.items():
        assert len(entradas) == 2, f"{alias} no tiene sus dos motores"
        a, b = entradas
        pa, pb = dict(a["litellm_params"]), dict(b["litellm_params"])
        ia, ib = dict(a["model_info"]), dict(b["model_info"])
        assert pa.pop("api_base") != pb.pop("api_base"), f"{alias}: los dos al mismo Service"
        assert ia.pop("id") != ib.pop("id"), f"{alias}: los dos gemelos con el mismo id"
        assert ia.pop("backend") != ib.pop("backend"), f"{alias}: los dos en el mismo nodo"
        # DGX-897: el tope por pod es lo otro que cambia, el `--max-num-seqs` de cada motor
        # entre las dos replicas del proxy (TP=2 8, TP=1 5 — DGX-955h).
        topes = {a["model_info"]["id"][-3:]: pa.pop("max_parallel_requests"),
                 b["model_info"]["id"][-3:]: pb.pop("max_parallel_requests")}
        assert topes == {"tp2": 4, "tp1": 3}, f"{alias}: topes por pod {topes}"
        assert pa == pb, f"{alias}: litellm_params divergen entre gemelos"
        assert ia == ib, f"{alias}: model_info diverge entre gemelos"


def test_los_ids_de_las_deployments_son_unicos_y_acaban_en_el_motor():
    """`model_info.id` es la clave de cooldown del Router, la del pin de backend y
    la de los health checks: duplicarla mezclaria dos motores en un mismo estado."""
    ids = [str((e.get("model_info") or {}).get("id") or "") for e in _config()["model_list"]]
    con_id = [i for i in ids if i]
    assert len(con_id) == len(set(con_id)), f"ids duplicados: {con_id}"
    gemelos = [i for i in con_id if i.endswith(GEM)]
    assert len(gemelos) == 4, gemelos          # residente y abliterado, en los dos motores
    assert "correo-clasificador" in con_id     # y el clasificador, clavado a SI


def test_la_url_de_metricas_del_despliegue_son_las_dos_del_pool():
    """El env es la verdad del cluster: si solo lista el TP=2, la valvula ve la
    mitad de la capacidad y desvia a Alibaba con el residente a medio usar."""
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    deploy = next(d for d in docs
                  if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm")
    env = {e["name"]: e.get("value", "") for e in deploy["spec"]["template"]["spec"]
           ["containers"][0]["env"]}
    urls = [u.strip() for u in env["SESSION_ROUTER_VLLM_METRICS_URL"].split(",") if u.strip()]
    assert len(urls) == 2, urls
    assert all(u.endswith("/metrics") for u in urls)
    assert any("-tp1." in u for u in urls), "falta el TP=1"


# ── DGX-974: las nuevas, al motor con mas plazas libres ──────────────────────

URL_TP2 = "http://qwen38-flash-next.llm.svc.cluster.local:8000/metrics"
URL_TP1 = "http://qwen38-flash-next-tp1.llm.svc.cluster.local:8000/metrics"


def _con_motor(deps):
    """Los gemelos con su `api_base` real: asi casa cada uno con su /metrics."""
    for d in deps:
        tp1 = d["model_info"]["id"].endswith("-tp1")
        host = "qwen38-flash-next-tp1" if tp1 else "qwen38-flash-next"
        d["litellm_params"]["api_base"] = f"http://{host}.llm.svc.cluster.local:8000/v1"
    return deps


def _carga(pod, tp2, tp1, edad=0.0):
    """Lo que leyo el sondeo de cada motor, (en marcha, en cola), hace `edad` s."""
    t = pod.time.monotonic() - edad
    pod._vllm_by_url.update({URL_TP2: (tp2[1], tp2[0], t), URL_TP1: (tp1[1], tp1[0], t)})
    pod._claims_since_read.clear()


def _elige(pod, sid):
    kwargs = _kwargs(sid) if sid else {"metadata": {"user_api_key_hash": KEY_A}}
    return _backend(run(pod.resident_backend_filter(
        FAMILIA[0], _con_motor(_gemelos(FAMILIA[0])), kwargs)))


def _reparto(pod, n=400):
    vistos = []
    for i in range(n):
        vistos.append(_elige(pod, f"ses-{i}"))
        pod._claims_since_read.clear()      # todas miran la misma lectura
    return vistos.count("tp2") / n


def test_la_nueva_va_al_motor_con_mas_plazas_libres():
    """El caso del 16-10, 07:53Z: el TP=2 lleno y con cola, el TP=1 vacio (DGX-955h:
    8 plazas el TP=2, 5 el TP=1)."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    _carga(pod, tp2=(8, 2), tp1=(0, 0))
    assert _elige(pod, "ses-nueva") == "tp1"
    assert valkey.data[pod.backend_pin_key(KEY_A, "ses-nueva")] == "tp1"
    assert pod.BACKEND_AFFINITY_STATS["by_load"] == 1
    _carga(pod, tp2=(2, 0), tp1=(3, 0))      # 6 libres frente a 2: el grande
    assert _elige(pod, "ses-otra") == "tp2"


def test_con_empate_o_sin_lectura_fresca_decide_el_peso_de_siempre():
    pod = _pod(FakeValkey())
    _carga(pod, tp2=(4, 0), tp1=(1, 0))      # 4 libres en los dos
    assert 0.64 < _reparto(pod) < 0.86
    pod = _pod(FakeValkey())
    _carga(pod, tp2=(8, 2), tp1=(0, 0), edad=pod.VLLM_STALE_SECONDS + 1)
    assert 0.64 < _reparto(pod) < 0.86, "una lectura vieja no decide"
    assert pod.BACKEND_AFFINITY_STATS["by_load"] == 0


def test_sin_valkey_decide_el_sorteo_que_coincide_entre_pods():
    """Sin SET NX que arbitre, dos replicas con lecturas distintas no deben ligar
    la misma conversacion a motores distintos: decide el sorteo determinista."""
    a, b = _pod(None), _pod(None)
    _carga(a, tp2=(8, 2), tp1=(0, 0))
    _carga(b, tp2=(0, 0), tp1=(5, 0))
    for i in range(50):
        assert _elige(a, f"ses-{i}") == _elige(b, f"ses-{i}")


def test_una_rafaga_entre_dos_lecturas_no_cae_entera_en_el_mismo_motor():
    pod = _pod(FakeValkey())
    _carga(pod, tp2=(6, 0), tp1=(1, 0))      # 2 libres frente a 4
    assert [_elige(pod, f"r-{i}") for i in range(2)] == ["tp1", "tp1"]
    assert pod._claims_since_read[URL_TP1] == 2
    # Con las dos plazas de ventaja ya dadas empatan y vuelve a decidir el peso.
    assert pod.least_loaded_backend({"tp2": _con_motor(_deps(FAMILIA[0], ("tp2",))),
                                     "tp1": _con_motor(_deps(FAMILIA[0], ("tp1",)))}) is None
    _carga(pod, tp2=(6, 0), tp1=(1, 0))      # la lectura siguiente pone el contador a 0
    assert pod._claims_since_read == {}


def test_la_ligada_no_se_mueve_aunque_su_motor_vaya_lleno():
    valkey = FakeValkey()
    pod = _pod(valkey)
    valkey.data[pod.backend_pin_key(KEY_A, "ses-vieja")] = "tp2"
    _carga(pod, tp2=(16, 2), tp1=(0, 0))
    assert _elige(pod, "ses-vieja") == "tp2"


def test_la_peticion_sin_sesion_tambien_va_al_motor_con_hueco():
    valkey = FakeValkey()
    pod = _pod(valkey)
    _carga(pod, tp2=(16, 2), tp1=(0, 0))
    assert _elige(pod, None) == "tp1"
    assert not valkey.data, "sin sid no hay conversacion que clavar"


@pytest.mark.parametrize("vivo, caido, url_vivo", [
    ("tp1", "tp2", URL_TP1),     # el corte de luz del 18-10: TP=2 sin enlace
    ("tp2", "tp1", URL_TP2),     # dgx3 en ventana creative o caido
])
def test_motor_que_no_contesta_su_metrics_sale_del_reparto(vivo, caido, url_vivo):
    """DGX-955h: se cae cualquiera de los dos y su /metrics deja de contestar.
    Las nuevas nacen en el vivo sin pin (no hubo eleccion) y la ligada al caido
    muda entera. Sin esto el sorteo 3:1 seguia mandando nuevas al muerto. Con
    los dos vivos reparte por plazas libres (tests de arriba); con ninguno, la
    admision llm-tp ya lo manda a Alibaba."""
    valkey = FakeValkey()
    pod = _pod(valkey)
    pod._vllm_by_url[url_vivo] = (0.0, 0.0, pod.time.monotonic())
    assert {_elige(pod, f"nueva-{i}") for i in range(40)} == {vivo}
    assert pod.backend_pin_key(KEY_A, "nueva-0") not in valkey.data
    assert _elige(pod, None) == vivo
    valkey.data[pod.backend_pin_key(KEY_A, "ses-vieja")] = caido
    assert _elige(pod, "ses-vieja") == vivo
    assert valkey.data[pod.backend_pin_key(KEY_A, "ses-vieja")] == vivo
