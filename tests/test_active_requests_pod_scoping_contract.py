"""SC-403: el fichero de peticiones en vuelo es por pod y el sidecar suma.

Que se comprueba aqui (los numeros son los criterios de aceptacion de SC-403;
los que se miden con peticiones reales en el cluster son de qa y no caben en
un test local):

  AC1  el volumen `tracking` es emptyDir y no queda ni una coincidencia de la
       ruta del nodo ni del tipo de volumen antiguo en el manifiesto.
  AC2  cada pod escribe su fichero y ninguno reescribe el del otro.
  AC3  Service headless con publishNotReadyAddresses: true.
  AC4  la agregacion llama al endpoint HOJA de los pares, exactamente una vez
       por par y por ciclo (ni recursion ni N al cuadrado).
  AC5  la forma del JSON de /internal/active-requests es IDENTICA a la de antes
       -- comparada contra el codigo de la version anterior, no contra un
       recuerdo.
  AC6  /local existe y convive con la URL vieja.
  AC8  un par inalcanzable degrada el numero, no la respuesta.
  AC9  el drenaje lee SU fichero y no espera a las peticiones del vecino.
  AC10 lo que no se toca sigue intacto (memoria, sondas, afinidad, preStop).

Los tres modulos embebidos se ejecutan con `exec` sobre el contenido real del
ConfigMap, igual que hacen tests/test_active_request_metrics_contract.py y
tests/test_compute_mode_admission_contract.py: el artefacto que se prueba es el
que se monta en el pod, no una copia.
"""
import contextlib
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
ACTIVE_BASE = "/shared/tracking/active_requests.json"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _config_data():
    for document in _docs():
        if (
            isinstance(document, dict)
            and document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name") == "litellm-config"
        ):
            return document["data"]
    raise AssertionError("litellm-config ConfigMap not found")


def _exec_source(source, name, env=None, overrides=None):
    """Ejecuta un fuente dado con `env` puesto y despues lo limpia.

    El `env` hace falta DURANTE el `exec` porque los modulos calculan sus
    rutas al importar (`LOCAL_ACTIVE_FILE` a partir de `LITELLM_ACTIVE_FILE` y
    `POD_NAME`); escribir la ruta DESPUES en el namespace no vale si el modulo
    ya la derivo — es justo el bug que dio el rojo de AC5 en CI del 09-09.

    `overrides` se escribe DESPUES en el namespace: sirve para sustituir
    globales del modulo (rutas, service de pares, stubs de red) sin depender de
    variables de entorno del proceso que corre pytest.
    """
    saved = dict(os.environ)
    try:
        os.environ.update(env or {})
        namespace = {"__name__": f"sc403_contract_{name.replace('.', '_')}"}
        exec(compile(source, name, "exec"), namespace)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    for key, value in (overrides or {}).items():
        namespace[key] = value
    return namespace


def _module(name, env=None, overrides=None):
    """Ejecuta un modulo embebido del manifiesto actual (ver `_exec_source`)."""
    return _exec_source(_config_data()[name], name, env, overrides)


@contextlib.contextmanager
def _pod_env(pod=None, **extra):
    """POD_NAME durante la LLAMADA.

    `_module` limpia el entorno al terminar el `exec`, y `resolve_active_file`
    lee POD_NAME en el momento en que se llama, no al importar: por eso las
    pruebas de la ruta necesitan el entorno puesto alrededor de la llamada.
    """
    saved = dict(os.environ)
    try:
        os.environ.update(extra)
        if pod:
            os.environ["POD_NAME"] = pod
        else:
            os.environ.pop("POD_NAME", None)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _record(request_id, ts):
    return {
        "request_id": request_id,
        "trace_id": None,
        "source": "litellm",
        "alias": "openclaw-qwen36-prod",
        "model": "tooling",
        "call_type": "acompletion",
        "server_id": "vllm-ornith-35b-nvfp4-dgx1",
        "refusal_lambda": None,
        "refusal_runtime": None,
        "ts": ts,
        "first_token_ts": None,
        "last_sample_ts": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "output_speed_tps": None,
        "ttft_ms": None,
    }


def _write_active_file(directory, name, records):
    path = Path(directory) / name
    path.write_text(json.dumps({row["request_id"]: row for row in records}))
    return str(path)


# ── AC1 / AC3 / AC10: forma del manifiesto ────────────────────────────────────


def test_ac1_el_volumen_tracking_es_empty_dir_y_no_queda_rastro_del_nodo():
    raw = MANIFEST.read_text()
    # El criterio es un grep: cero coincidencias. Incluye los comentarios, asi
    # que ni el texto explicativo puede escribir esas dos palabras.
    assert "hostPath" not in raw
    assert "dgx-tracking" not in raw

    deployment = [d for d in _docs() if d.get("kind") == "Deployment"][0]
    volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
    assert volumes["tracking"] == {"name": "tracking", "emptyDir": {}}

    containers = {c["name"]: c for c in deployment["spec"]["template"]["spec"]["containers"]}
    litellm_mounts = {m["name"]: m for m in containers["litellm"]["volumeMounts"]}
    sidecar_mounts = {m["name"]: m for m in containers["active-requests-api"]["volumeMounts"]}
    assert litellm_mounts["tracking"] == {"name": "tracking", "mountPath": "/shared/tracking"}
    assert sidecar_mounts["tracking"] == {
        "name": "tracking",
        "mountPath": "/shared/tracking",
        "readOnly": True,
    }
    # AC1 tambien fija la variable de entorno: misma base en los dos contenedores.
    for name in ("litellm", "active-requests-api"):
        env = {e["name"]: e for e in containers[name]["env"]}
        assert env["LITELLM_ACTIVE_FILE"]["value"] == ACTIVE_BASE


def test_ac3_service_headless_de_pares_con_publish_not_ready():
    services = {d["metadata"]["name"]: d for d in _docs() if d.get("kind") == "Service"}
    assert "litellm-active-peers" in services
    peers = services["litellm-active-peers"]["spec"]
    assert peers["clusterIP"] == "None"
    assert peers["publishNotReadyAddresses"] is True
    assert peers["selector"] == {"app": "litellm"}
    assert peers["ports"] == [{"name": "active", "port": 4001, "targetPort": 4001}]

    # El Service de trafico NO publica not-ready: ahi si importa que un pod que
    # se vacia deje de recibir inferencia.
    assert services["litellm"]["spec"].get("publishNotReadyAddresses") is None


def test_ac10_lo_que_no_se_toca_sigue_intacto():
    deployment = [d for d in _docs() if d.get("kind") == "Deployment"][0]
    template = deployment["spec"]["template"]["spec"]
    containers = {c["name"]: c for c in template["containers"]}
    litellm = containers["litellm"]

    assert deployment["spec"]["replicas"] == 2
    assert litellm["resources"]["limits"]["memory"] == "6Gi"
    assert litellm["startupProbe"]["httpGet"]["path"] == "/health/liveliness"
    assert litellm["livenessProbe"]["httpGet"]["path"] == "/health/liveliness"
    assert litellm["lifecycle"]["preStop"]["exec"]["command"] == [
        "python",
        "/config/litellm_drain.py",
    ]
    env = {e["name"]: e["value"] for e in litellm["env"] if "value" in e}
    assert env["LITELLM_DRAIN_TIMEOUT_SEC"] == "660"

    # AC10 es tambien un "no se ensancha": la afinidad sigue anclada a ubuntu
    # (eso es H3/SC-404, y un PR que la ensanche aqui se rechaza).
    terms = template["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    assert terms[0]["matchExpressions"] == [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["ubuntu"]}
    ]


# ── AC2: un escritor por fichero ─────────────────────────────────────────────


@pytest.mark.parametrize("pod", ["litellm-986845cb7-88cfl", "litellm-986845cb7-r7vbf"])
def test_la_ruta_efectiva_lleva_el_pod_dentro(pod):
    tracking = _module("active_request_tracking.py")
    with _pod_env(pod):
        path = tracking["resolve_active_file"](ACTIVE_BASE)
    assert path == f"/shared/tracking/active_requests_{pod}.json"
    assert path != ACTIVE_BASE


def test_sin_pod_name_la_ruta_es_la_de_siempre():
    tracking = _module("active_request_tracking.py")
    assert tracking["resolve_active_file"](ACTIVE_BASE) == ACTIVE_BASE


def test_las_tres_implementaciones_del_nombre_coinciden():
    """El duplicado esta controlado: drain y sidecar calculan lo mismo.

    No importan active_request_tracking.py (el sidecar corre en python:3.12-slim
    y el drain en un preStop; ninguno va a depender del modulo del hook), asi
    que la unica garantia de que no diverjan es comprobarlo aqui.
    """
    tracking = _module("active_request_tracking.py")
    drain = _module("litellm_drain.py")
    sidecar = _module("active_requests_api.py")

    for pod in ["", "litellm-abc-1", "litellm-abc-2"]:
        for base in (ACTIVE_BASE, "/tmp/otro_directorio/otro_nombre.jsonl"):
            os.environ.pop("POD_NAME", None)
            if pod:
                os.environ["POD_NAME"] = pod
            try:
                expected = tracking["resolve_active_file"](base)
                assert drain["_resolve_active_file"](base) == expected, (pod, base)
                assert sidecar["_resolve_active_file"](base) == expected, (pod, base)
            finally:
                os.environ.pop("POD_NAME", None)


def test_ac2_dos_pods_no_se_pisan_el_fichero():
    tracking = _module("active_request_tracking.py")
    with tempfile.TemporaryDirectory() as temp_dir:
        path_a = f"{temp_dir}/active_requests_pod-a.json"
        path_b = f"{temp_dir}/active_requests_pod-b.json"
        tracker_a = tracking["ActiveRequestTracker"](path_a)
        tracker_b = tracking["ActiveRequestTracker"](path_b)

        tracker_a.start("req-en-a", key_alias="alias-a", model="tooling",
                        call_type="acompletion", api_base=None)

        assert Path(path_b).exists() is False
        assert [r["request_id"] for r in json.loads(Path(path_a).read_text()).values()] == [
            "req-en-a"
        ]

        tracker_b.start("req-en-b", key_alias="alias-b", model="tooling",
                        call_type="acompletion", api_base=None)
        assert [r["request_id"] for r in json.loads(Path(path_a).read_text()).values()] == [
            "req-en-a"
        ]
        assert [r["request_id"] for r in json.loads(Path(path_b).read_text()).values()] == [
            "req-en-b"
        ]


# ── AC9: el drenaje espera lo suyo ───────────────────────────────────────────


def test_ac9_el_drenaje_cuenta_solo_las_peticiones_de_su_pod():
    with tempfile.TemporaryDirectory() as temp_dir:
        now = __import__("time").time()
        file_a = _write_active_file(
            temp_dir, "active_requests_pod-a.json", [_record("a-1", now), _record("a-2", now)]
        )
        file_b = _write_active_file(temp_dir, "active_requests_pod-b.json", [_record("b-1", now)])

        drain = _module("litellm_drain.py", {
            "POD_NAME": "pod-a",
            "LITELLM_ACTIVE_FILE": f"{temp_dir}/active_requests.json",
        })
        assert drain["ACTIVE_FILE"] == file_a
        assert drain["active_count"]() == 2

        # Vaciar A no espera a las de B: A pasa a 0 con B intacto.
        Path(file_a).write_text(json.dumps({}))
        assert drain["active_count"]() == 0
        assert drain["active_count"](file_b) == 1


def test_el_drenaje_filtra_anticuados_y_aguanta_fichero_roto():
    with tempfile.TemporaryDirectory() as temp_dir:
        now = __import__("time").time()
        fresh = _write_active_file(temp_dir, "fresh.json", [_record("f", now)])
        old = _write_active_file(temp_dir, "old.json", [_record("o", now - 4000)])
        broken = Path(temp_dir) / "broken.json"
        broken.write_text("{no es json")

        drain = _module("litellm_drain.py")
        assert drain["active_count"](fresh) == 1
        assert drain["active_count"](old) == 0
        assert drain["active_count"](str(broken)) == 0
        assert drain["active_count"](f"{temp_dir}/inexistente.json") == 0


# ── AC4: la agregacion llama a la hoja, una vez por par ──────────────────────


class _FakeSocket:
    """getaddrinfo del service headless: una IP por pod, con duplicados dentro."""

    SOCK_STREAM = 1

    def __init__(self, addresses):
        self._addresses = addresses
        self.calls = 0

    def getaddrinfo(self, host, port, type=None):
        self.calls += 1
        return [
            (2, 1, 6, "", (address, port)) for address in self._addresses
        ]


def test_descubrimiento_deduplica_y_descarta_la_ip_propia():
    sidecar = _module("active_requests_api.py", {"POD_NAME": "pod-a", "POD_IP": "10.42.0.7"})
    sidecar["PEER_SERVICE"] = "litellm-active-peers.litellm.svc.cluster.local"
    fake = _FakeSocket(["10.42.0.7", "10.42.0.8", "10.42.0.8", "10.42.0.9"])
    sidecar["socket"] = fake
    assert sidecar["discover_peer_addresses"]() == ["10.42.0.8", "10.42.0.9"]
    assert fake.calls == 1


def test_ac4_una_sola_llamada_de_par_por_ciclo_y_solo_a_la_hoja():
    sidecar = _module("active_requests_api.py", {"POD_NAME": "pod-a", "POD_IP": "10.42.0.1"})
    sidecar["LOCAL_ACTIVE_FILE"] = "/inexistente/para-el-test.json"
    sidecar["PEER_SERVICE"] = "litellm-active-peers.litellm.svc.cluster.local"
    sidecar["discover_peer_addresses"] = lambda: ["10.42.0.2", "10.42.0.3", "10.42.0.4"]

    called = []

    def fake_fetch(address):
        called.append(address)
        return address, [], None

    sidecar["fetch_peer_local"] = fake_fetch

    sidecar["aggregate_active"]()
    assert sorted(called) == ["10.42.0.2", "10.42.0.3", "10.42.0.4"]

    called.clear()
    sidecar["aggregate_active"]()
    assert sorted(called) == ["10.42.0.2", "10.42.0.3", "10.42.0.4"]


def test_la_url_del_par_es_el_endpoint_hoja_y_lleva_credential():
    """AC4 en la parte que importa: jamas se llama al agregado del par."""
    sidecar = _module("active_requests_api.py", {"LITELLM_MASTER_KEY": "clave-maestra"})
    seen = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"active": []}'

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, request.get_header("Authorization")))
        return _Response()

    original = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        address, rows, error = sidecar["fetch_peer_local"]("10.42.0.9")
    finally:
        urllib.request.urlopen = original

    assert error is None and rows == []
    url, header = seen[0]
    assert url == "http://10.42.0.9:4001/internal/active-requests/local"
    assert url != "http://10.42.0.9:4001/internal/active-requests"
    assert header == "Bearer clave-maestra"


# ── Merge, dedupe, filtro de antiguedad ──────────────────────────────────────


def _normalized_row(request_id, ts):
    sidecar = _module("active_requests_api.py")
    return sidecar["_normalize"]({"active": [_record(request_id, ts)]})[0]


def test_la_suma_es_union_y_deduplica_por_request_id():
    now = __import__("time").time()
    with tempfile.TemporaryDirectory() as temp_dir:
        mine = _write_active_file(temp_dir, "mine.json", [_record("a-1", now), _record("shared", now)])
        sidecar = _module("active_requests_api.py", {"POD_NAME": "pod-a"})
        sidecar["LOCAL_ACTIVE_FILE"] = mine
        sidecar["PEER_SERVICE"] = "litellm-active-peers.litellm.svc.cluster.local"
        sidecar["discover_peer_addresses"] = lambda: ["10.42.0.2"]
        sidecar["fetch_peer_local"] = lambda address: (
            address,
            [_normalized_row("shared", now), _normalized_row("b-1", now)],
            None,
        )

        payload = sidecar["aggregate_active"]()
        ids = [row["request_id"] for row in payload["active"]]
        assert sorted(ids) == ["a-1", "b-1", "shared"]
        assert len(ids) == len(set(ids)), "el dedupe por request_id falla"
        assert "peers_failed" not in payload


def test_los_filas_del_par_se_normalizan_y_se_filtran_por_antiguedad():
    """El filtro de 900 s se aplica tambien a lo que trae un par.

    Se prueba en `fetch_peer_local`, que es donde entra el payload ajeno: se
    re-normaliza con las claves de siempre y la edad se mide con el reloj de
    quien agrega, no con el del par.
    """
    now = __import__("time").time()
    body = json.dumps({
        "active": [
            {**_record("b-fresco", now), "key_alias": "opencode-20260630-local"},
            _record("b-viejo", now - 4000),
        ]
    }).encode()
    sidecar = _module("active_requests_api.py")

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    original = urllib.request.urlopen
    urllib.request.urlopen = lambda request, timeout=None: _Response()
    try:
        address, rows, error = sidecar["fetch_peer_local"]("10.42.0.2")
    finally:
        urllib.request.urlopen = original

    assert error is None
    assert [row["request_id"] for row in rows] == ["b-fresco"]
    assert rows[0]["key_alias"] == "opencode-20260630-local"
    assert isinstance(rows[0]["age_seconds"], int)


def test_ac8_un_par_inalcanzable_degrada_el_numero_sin_romper_nada(capsys):
    now = __import__("time").time()
    with tempfile.TemporaryDirectory() as temp_dir:
        mine = _write_active_file(temp_dir, "mine.json", [_record("a-1", now)])
        sidecar = _module("active_requests_api.py", {"POD_NAME": "pod-a"})
        sidecar["LOCAL_ACTIVE_FILE"] = mine
        sidecar["PEER_SERVICE"] = "litellm-active-peers.litellm.svc.cluster.local"
        sidecar["discover_peer_addresses"] = lambda: ["10.42.0.2", "10.42.0.3"]

        def fetch(address):
            if address == "10.42.0.3":
                raise OSError("no route to host")
            return address, [_normalized_row("b-1", now)], None

        def fetch_wrapped(address):
            try:
                return fetch(address)
            except Exception as exc:  # como hace el real: el error sale en la tupla
                return address, [], f"{type(exc).__name__}: {exc}"

        sidecar["fetch_peer_local"] = fetch_wrapped
        payload = sidecar["aggregate_active"]()

        assert [row["request_id"] for row in payload["active"]] == ["a-1", "b-1"]
        assert [entry["peer"] for entry in payload["peers_failed"]] == ["10.42.0.3"]
        assert "10.42.0.3" in capsys.readouterr().err


def test_la_resolucion_de_pares_colgada_no_excede_el_presupuesto():
    """El dashboard expira su llamada a los 2 s: una DNS colgada degrada, no bloquea."""
    import time as _time

    sidecar = _module("active_requests_api.py")
    sidecar["PEER_SERVICE"] = "litellm-active-peers.litellm.svc.cluster.local"
    sidecar["PEER_TIMEOUT_SEC"] = 0.2

    def slow_discovery():
        _time.sleep(5)
        return ["10.42.0.2"]

    sidecar["discover_peer_addresses"] = slow_discovery

    started = _time.time()
    assert sidecar["_discover_peer_addresses_bounded"]() == []
    assert _time.time() - started < 1.5


def test_sin_service_de_pares_no_se_llama_a_nadie():
    with tempfile.TemporaryDirectory() as temp_dir:
        now = __import__("time").time()
        mine = _write_active_file(temp_dir, "mine.json", [_record("a-1", now)])
        sidecar = _module("active_requests_api.py", {"POD_NAME": "pod-a"})
        sidecar["LOCAL_ACTIVE_FILE"] = mine
        sidecar["PEER_SERVICE"] = ""

        def boom(address):
            raise AssertionError("no debe llamarse a ningun par")

        sidecar["fetch_peer_local"] = boom
        payload = sidecar["aggregate_active"]()
        assert [row["request_id"] for row in payload["active"]] == ["a-1"]
        assert "peers_failed" not in payload


# ── AC5 / AC6: contrato por HTTP ─────────────────────────────────────────────


def _serve(module_namespace):
    server = ThreadingHTTPServer(("127.0.0.1", 0), module_namespace["Handler"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _get_json(url, token=None):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def _shape(value):
    """Forma recursiva: claves ordenadas y tipos, sin valores."""
    if isinstance(value, dict):
        return {key: _shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


OLD_SIDECAR_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "active_requests_api_pre-sc403.py"


def _old_sidecar_source():
    """El codigo de ANTES del cambio, fijado en un fixture, no leido de git.

    Leyendolo de `origin/main`/`main` el test era dependiente del entorno del
    runner, y en dos direcciones:

      * El checkout de CI (`actions/checkout`, `fetch-depth: 1`, solo la ref de
        la PR) no tiene NI `origin/main` NI `main`; el fallback a `HEAD`
        devolvia el codigo NUEVO disfrazado de viejo — que ademas ya habia
        calculado `LOCAL_ACTIVE_FILE` al importar, asi que el override de
        `ACTIVE_FILE` no se enteraba y servia `active: []`. Rojo en CI y verde
        en local (donde `origin/main` si existe). Reproducido tal cual en un
        clone `--depth 1 --branch` de esta rama.
      * Fusionado SC-403, `origin/main` YA es el codigo nuevo: el test se
        quedaba comparando nuevo contra nuevo para siempre.

    El contrato de AC5 es contra la forma que consumia el dashboard ANTES de
    SC-403, y esa forma es fija. El fixture es el blob exacto de
    `git show origin/main:k8s/manifest.yaml` en la base de la rama (f225a18),
    extraido con yaml del ConfigMap y no a mano. Si algun dia la forma cambia a
    proposito, se actualiza el fixture JUNTO con los consumidores (dgx-infra
    `api/litellm_active.py` y `api/routes_hud.py`), nunca por separado.
    """
    return OLD_SIDECAR_FIXTURE.read_text(encoding="utf-8")


def test_ac5_la_forma_del_agregado_es_identica_a_la_de_antes():
    """Misma URL, mismas claves, mismos tipos. Lo unico que cambia es el numero."""
    now = __import__("time").time()
    with tempfile.TemporaryDirectory() as temp_dir:
        records = [_record("req-1", now - 3)]
        # El MISMO contenido en las dos rutas que lee cada lado, y cada lado
        # puesto en su ruta via su propia variable de entorno al importar: el
        # viejo lee la base; el nuevo, con POD_NAME, la sufijada — la que
        # calcula `_resolve_active_file` de verdad, no un override a mano
        # despues de importar (que era como se colaba el `active: []` de CI).
        base_path = _write_active_file(temp_dir, "active_requests.json", records)
        _write_active_file(temp_dir, "active_requests_pod-a.json", records)

        # La forma de referencia es el codigo de la version anterior ejecutado
        # tal cual, no lo que yo recuerde del diff.
        old = _exec_source(
            _old_sidecar_source(),
            "active_requests_api.py",
            env={"LITELLM_ACTIVE_FILE": base_path},
        )
        old_server, old_url = _serve(old)
        new = _module(
            "active_requests_api.py",
            {"POD_NAME": "pod-a", "LITELLM_ACTIVE_FILE": base_path},
            {"PEER_SERVICE": ""},
        )
        assert new["LOCAL_ACTIVE_FILE"] == str(Path(temp_dir) / "active_requests_pod-a.json")
        new_server, new_url = _serve(new)

        try:
            status_old, body_old = _get_json(f"{old_url}/internal/active-requests")
            status_new, body_new = _get_json(f"{new_url}/internal/active-requests")
        finally:
            old_server.shutdown()
            new_server.shutdown()

        assert status_old == status_new == 200
        # Las dos bandejas tienen que venir LLENAS y sin error de lectura: un
        # lado que no recibe el fixture cae en el FileNotFoundError y sirve
        # `active: []` SIN clave `error`, y la comparacion de formas pasaria
        # comparando una lista vacia contra una fila.
        assert body_old["active"] and body_new["active"], "un lado no recibio el fixture"
        assert "error" not in body_old and "error" not in body_new
        assert _shape(body_old) == _shape(body_new), (
            f"la forma cambio:\nantes: {json.dumps(_shape(body_old))}\n"
            f"ahora: {json.dumps(_shape(body_new))}"
        )
        assert body_old["active"][0]["request_id"] == body_new["active"][0]["request_id"] == "req-1"


def test_ac6_local_y_la_url_vieja_conviven_y_piden_credential():
    now = __import__("time").time()
    with tempfile.TemporaryDirectory() as temp_dir:
        mine = _write_active_file(temp_dir, "mine.json", [_record("a-1", now)])
        sidecar = _module("active_requests_api.py",
                          {"POD_NAME": "pod-a", "LITELLM_MASTER_KEY": "clave-maestra"})
        sidecar["LOCAL_ACTIVE_FILE"] = mine
        sidecar["PEER_SERVICE"] = ""
        server, url = _serve(sidecar)
        try:
            status_local, body_local = _get_json(
                f"{url}/internal/active-requests/local", "clave-maestra")
            status_agg, body_agg = _get_json(
                f"{url}/internal/active-requests", "clave-maestra")
            status_health, _ = _get_json(f"{url}/healthz")
            status_no_auth, _ = _get_json(f"{url}/internal/active-requests/local")
            status_bad_auth, _ = _get_json(f"{url}/internal/active-requests", "equivocada")
            status_404, _ = _get_json(f"{url}/internal/active-requests/otro", "clave-maestra")
        finally:
            server.shutdown()

        assert status_local == 200
        assert [row["request_id"] for row in body_local["active"]] == ["a-1"]
        assert body_local["pod"] == "pod-a"
        assert status_agg == 200
        assert [row["request_id"] for row in body_agg["active"]] == ["a-1"]
        assert status_health == 200
        assert status_no_auth == 401
        assert status_bad_auth == 401
        assert status_404 == 404


def test_un_fallo_inesperado_agregando_degada_a_local_y_no_devuelve_500(capsys):
    """Nunca un 500: el dashboard lo daria por fallido y caeria al fallback rancio."""
    now = __import__("time").time()
    with tempfile.TemporaryDirectory() as temp_dir:
        mine = _write_active_file(temp_dir, "mine.json", [_record("a-1", now)])
        sidecar = _module("active_requests_api.py", {"POD_NAME": "pod-a"})
        sidecar["LOCAL_ACTIVE_FILE"] = mine
        sidecar["PEER_SERVICE"] = "litellm-active-peers.litellm.svc.cluster.local"

        def boom():
            raise RuntimeError("algo que no esperabamos")

        sidecar["aggregate_active"] = boom
        server, url = _serve(sidecar)
        try:
            status, body = _get_json(f"{url}/internal/active-requests")
        finally:
            server.shutdown()

        assert status == 200
        assert [row["request_id"] for row in body["active"]] == ["a-1"]
        assert "algo que no esperabamos" in body["error"]
        assert "aggregation failed" in capsys.readouterr().err


def test_una_variable_de_tiempos_mal_escrita_no_tumba_el_sidecar():
    """El sidecar da el servicio de descubrimiento: crashloop = pod not-ready."""
    sidecar = _module(
        "active_requests_api.py",
        {"LITELLM_ACTIVE_PEER_TIMEOUT_SEC": "un-segundo-mas-o-menos"},
    )
    assert sidecar["PEER_TIMEOUT_SEC"] == 1.0


def test_el_agregado_sirve_los_datos_de_los_pares_vivos_por_http():
    """Dos sidecares reales, uno detras de otro: la suma por HTTP, no por fichero."""
    now = __import__("time").time()
    with tempfile.TemporaryDirectory() as temp_dir:
        file_a = _write_active_file(temp_dir, "active_requests_pod-a.json", [_record("a-1", now)])
        file_b = _write_active_file(temp_dir, "active_requests_pod-b.json",
                                    [_record("b-1", now), _record("b-2", now)])

        pod_b = _module("active_requests_api.py", {"POD_NAME": "pod-b"})
        pod_b["LOCAL_ACTIVE_FILE"] = file_b
        pod_b["PEER_SERVICE"] = ""
        server_b, url_b = _serve(pod_b)

        pod_a = _module("active_requests_api.py", {"POD_NAME": "pod-a"})
        pod_a["LOCAL_ACTIVE_FILE"] = file_a
        pod_a["PEER_SERVICE"] = "litellm-active-peers.litellm.svc.cluster.local"
        host_b, port_b = url_b.rsplit(":", 1)
        pod_a["discover_peer_addresses"] = lambda: [host_b.split("//")[1]]
        pod_a["PEER_PORT"] = int(port_b)

        try:
            server_a, url_a = _serve(pod_a)
            try:
                _, body = _get_json(f"{url_a}/internal/active-requests")
            finally:
                server_a.shutdown()
        finally:
            server_b.shutdown()

        assert sorted(row["request_id"] for row in body["active"]) == ["a-1", "b-1", "b-2"]
        assert "peers_failed" not in body


# El hash de la anotacion config.k8s.e-dani.com/revision NO se comprueba aqui:
# lo vigila tests/test_configmap_revision_bump_contract.py, que corre en la
# misma tanda y es el dueno de ese contrato.
