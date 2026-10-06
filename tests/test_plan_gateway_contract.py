"""plan-gateway (DGX-621): sitio unico de la media del Token Plan de Alibaba.

Contrato `dgx.alibaba.plan-gateway.v1`. El codigo del gateway vive INLINE en el
ConfigMap `plan-gateway-config` del manifiesto (el manifiesto manda, no hay copia
en dev/) y aqui se carga de ahi, junto con el `session_router.py` de
`litellm-config` que importa. El upstream es un `httpx.MockTransport` colgado del
ASGI: ningun socket, ninguna key real.

Criterios (spec DGX-621): forma del Deployment/Service, C2 (una sola
implementacion del selector), C4 (failover), C5 (ledger), allowlist, auth por
consumidor, tareas sin estado.
"""
import ast
import asyncio
import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
DOCS = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _doc(kind, name):
    return next(d for d in DOCS if d.get("kind") == kind and d["metadata"]["name"] == name)


GATEWAY_SRC = _doc("ConfigMap", "plan-gateway-config")["data"]["plan_gateway.py"]
ROUTER_SRC = _doc("ConfigMap", "litellm-config")["data"]["session_router.py"]

MEDIA = "/api/v1/services/aigc/multimodal-generation/generation"
VIDEO = "/api/v1/services/aigc/video-generation/video-synthesis"
TTS = "/api/v1/services/audio/tts/SpeechSynthesizer"
STUDIO = {"Authorization": "Bearer tok-studio"}


# --------------------------------------------------------------------------- forma


def test_deployment_plan_gateway_forma():
    dep = _doc("Deployment", "plan-gateway")
    spec = dep["spec"]
    assert spec["replicas"] == 2
    assert spec["strategy"] == {
        "type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
    }
    assert spec["selector"]["matchLabels"] == {"app": "plan-gateway"}
    pod = spec["template"]
    assert pod["metadata"]["labels"]["app"] == "plan-gateway"  # NUNCA app: litellm
    assert pod["spec"]["terminationGracePeriodSeconds"] == 150
    [c] = pod["spec"]["containers"]
    assert c["resources"]["requests"] == {"cpu": "50m", "memory": "128Mi"}
    assert c["resources"]["limits"] == {"memory": "512Mi"}
    terms = pod["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert terms == [{"matchExpressions": [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["ubuntu", "sauvage"]}]}]


def test_pdb_y_service_del_gateway():
    pdb = _doc("PodDisruptionBudget", "plan-gateway")
    assert pdb["spec"]["maxUnavailable"] == 1
    assert pdb["spec"]["selector"] == {"matchLabels": {"app": "plan-gateway"}}
    svc = _doc("Service", "plan-gateway")
    assert svc["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert svc["spec"]["selector"] == {"app": "plan-gateway"}
    assert [p["port"] for p in svc["spec"]["ports"]] == [4002]


def test_el_gateway_no_sale_del_cluster_ni_se_cuela_en_el_proxy():
    for d in DOCS:
        if d.get("kind") == "IngressRoute":
            assert "plan-gateway" not in json.dumps(d), "el gateway no lleva IngressRoute"
    for d in DOCS:
        if d.get("kind") == "Service" and d["metadata"]["name"] != "plan-gateway":
            assert 4002 not in [p["port"] for p in d["spec"].get("ports", [])], d["metadata"]["name"]
    assert 4002 not in [
        p["containerPort"] for c in _doc("Deployment", "litellm")["spec"]["template"]["spec"]["containers"]
        for p in c.get("ports", [])
    ]


def test_imagen_y_comando_del_gateway():
    gw = _doc("Deployment", "plan-gateway")["spec"]["template"]["spec"]["containers"][0]
    proxy = next(c for c in _doc("Deployment", "litellm")["spec"]["template"]["spec"]["containers"]
                 if c["name"] == "litellm")
    assert gw["image"] == proxy["image"] and "@sha256:" in gw["image"]
    cmd = " ".join(gw["command"])
    assert cmd.startswith("python -m uvicorn --app-dir /gw plan_gateway:app --host 0.0.0.0 --port 4002")
    assert "--timeout-graceful-shutdown 120" in cmd
    mounts = {m["mountPath"]: m for m in gw["volumeMounts"]}
    assert mounts["/gw/plan_gateway.py"]["subPath"] == "plan_gateway.py"
    assert mounts["/gw/session_router.py"]["subPath"] == "session_router.py"
    volumes = {v["name"]: v for v in _doc("Deployment", "plan-gateway")["spec"]["template"]["spec"]["volumes"]}
    assert volumes[mounts["/gw/plan_gateway.py"]["name"]]["configMap"]["name"] == "plan-gateway-config"
    assert volumes[mounts["/gw/session_router.py"]["name"]]["configMap"]["name"] == "litellm-config"


def test_tokens_por_consumidor_en_forma_connect():
    es = _doc("ExternalSecret", "plan-gateway-tokens")
    assert es["metadata"]["namespace"] == "litellm"
    assert es["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "onepassword-connect"}
    got = {d["secretKey"]: d["remoteRef"] for d in es["spec"]["data"]}
    assert got == {
        "PLAN_GATEWAY_TOKEN_STUDIO": {"key": "plan-gateway-studio", "property": "password"},
        "PLAN_GATEWAY_TOKEN_OMNIVOICE": {"key": "plan-gateway-omnivoice", "property": "password"},
    }
    env = {e["name"]: e for e in
           _doc("Deployment", "plan-gateway")["spec"]["template"]["spec"]["containers"][0]["env"]}
    for name in got:
        assert env[name]["valueFrom"]["secretKeyRef"]["name"] == "plan-gateway-tokens"
    # las dos keys del plan: los mismos Secrets que el proxy
    assert env["DASHSCOPE_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "litellm-alibaba"
    assert env["DASHSCOPE_API_KEY_2"]["valueFrom"]["secretKeyRef"]["name"] == "litellm-alibaba-2"


# --------------------------------------------------------------------------- C2


def test_c2_el_gateway_no_define_formula_de_eleccion():
    tree = ast.parse(GATEWAY_SRC)
    definidas = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "preferred_account" not in definidas
    importados = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            importados |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            importados.add((n.module or "").split(".")[0])
    assert not importados & {"hashlib", "random"}, importados
    assert "session_router" in importados
    llamadas = {
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "session_router"
    }
    # unica llamada al selector = draw_account; warm_config es el calentamiento del lifespan
    assert llamadas == {"draw_account", "warm_config"}, llamadas
    internos = {"preferred_account", "_account_weights", "_config", "_refresh_config"}
    tocados = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not tocados & internos, tocados & internos


def _cargar(tmp_path, monkeypatch, litellm_ausente=False):
    (tmp_path / "plan_gateway.py").write_text(GATEWAY_SRC)
    (tmp_path / "session_router.py").write_text(ROUTER_SRC)
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ("plan_gateway", "session_router"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    if litellm_ausente:
        monkeypatch.setitem(sys.modules, "litellm", None)  # `import litellm` => ImportError
    return importlib.import_module("plan_gateway")


def test_c2_session_router_y_gateway_se_importan_sin_litellm(tmp_path, monkeypatch):
    """La imagen trae litellm, pero el selector no puede depender de el: el gateway
    solo necesita httpx. Con `litellm` imposible de importar ambos cargan."""
    mod = _cargar(tmp_path, monkeypatch, litellm_ausente=True)
    assert callable(mod.session_router.draw_account) and callable(mod.session_router.warm_config)
    for name in ("plan_gateway", "session_router"):
        sys.modules.pop(name, None)


# --------------------------------------------------------------------------- harness


class _Body(httpx.AsyncByteStream):
    """Cuerpo sin consumir, como el de un transporte real (aiter_raw lo exige)."""

    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def reply(status, body=b"", ctype="application/json", headers=None, chunks=None):
    h = {"content-type": ctype, **(headers or {})}
    return httpx.Response(status, headers=h, stream=_Body(*(chunks or [body])))


def reply_json(status, obj, **kw):
    return reply(status, json.dumps(obj).encode(), **kw)


@pytest.fixture
def gw(tmp_path, monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("PLAN_GATEWAY_TOKEN_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-k1")
    monkeypatch.setenv("DASHSCOPE_API_KEY_2", "sk-k2")
    monkeypatch.setenv("PLAN_GATEWAY_TOKEN_STUDIO", "tok-studio")
    monkeypatch.setenv("PLAN_GATEWAY_TOKEN_OMNIVOICE", "tok-omni")
    mod = _cargar(tmp_path, monkeypatch)
    mod.draw_first = "k1"
    mod.real_draw = mod.session_router.draw_account

    async def draw(accounts):
        assert sorted(accounts) == ["k1", "k2"]
        return mod.draw_first

    monkeypatch.setattr(mod.session_router, "draw_account", draw)
    yield mod
    for name in ("plan_gateway", "session_router"):
        sys.modules.pop(name, None)


class Upstream:
    """Upstream falso: cada llamada se guarda y la respuesta la decide `script`."""

    def __init__(self, script):
        self.script = script
        self.seen = []

    def __call__(self, request):
        self.seen.append(request)
        out = self.script(request, len(self.seen))
        if isinstance(out, Exception):
            raise out
        return out

    @property
    def accounts(self):
        return [r.headers["authorization"].removeprefix("Bearer sk-") for r in self.seen]


def call(gw, upstream, method, path, headers=None, content=b""):
    async def run():
        gw._client = None  # el cliente cacheado seria de otro bucle y otro upstream
        gw._transport = httpx.MockTransport(upstream)
        transport = httpx.ASGITransport(app=gw.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            return await client.request(method, path, headers=headers or STUDIO, content=content)

    return asyncio.run(run())


def ledger(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]


# --------------------------------------------------------------------------- allowlist y auth


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/healthz"), ("POST", "/api/v1/tasks/abc"), ("GET", MEDIA),
    ("POST", "/compatible-mode/v1/chat/completions"), ("GET", "/api/v1/tasks/a/b"),
    ("GET", "/api/v1/tasks/"), ("POST", MEDIA + "/x"), ("DELETE", MEDIA), ("GET", "/api/v1/tasks/..%2Fx"),
])
def test_fuera_de_la_allowlist_404_sin_tocar_el_upstream(gw, method, path):
    up = Upstream(lambda r, n: reply_json(200, {}))
    assert call(gw, up, method, path).status_code == 404
    assert up.seen == []


def test_sin_token_o_con_token_malo_401(gw):
    up = Upstream(lambda r, n: reply_json(200, {}))
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "tok-studio"},
                    {"Authorization": "Basic tok-studio"}, {"Authorization": "Bearer "}):
        assert call(gw, up, "POST", MEDIA, headers=headers or {"x": "y"}).status_code == 401
    assert up.seen == []


def test_cada_consumidor_su_token_y_el_caller_es_su_nombre(gw, capsys):
    up = Upstream(lambda r, n: reply_json(200, {"ok": 1}))
    assert call(gw, up, "POST", MEDIA, headers={"Authorization": "Bearer tok-omni"}).status_code == 200
    assert call(gw, up, "POST", MEDIA, headers=STUDIO).status_code == 200
    assert [r["caller"] for r in ledger(capsys)] == ["omnivoice", "studio"]


def test_headers_se_reponen_se_descartan_y_pasan(gw):
    up = Upstream(lambda r, n: reply_json(200, {}))
    call(gw, up, "POST", VIDEO, content=b'{"model":"happyhorse-1.1-t2v"}', headers={
        **STUDIO, "X-Plan-Account": "k2", "X-DashScope-Async": "enable", "X-DashScope-SSE": "disable",
        "Content-Type": "application/json", "Connection": "x-secreto", "X-Secreto": "no", "Keep-Alive": "1",
        "TE": "trailers",
    })
    [req] = up.seen
    assert req.headers["authorization"] == "Bearer sk-k1"      # repuesta con la key de la cuenta
    assert "x-plan-account" not in req.headers                  # en un POST se descarta
    assert req.headers["x-dashscope-async"] == "enable" and req.headers["x-dashscope-sse"] == "disable"
    assert req.headers["content-type"] == "application/json"
    assert "x-secreto" not in req.headers and "keep-alive" not in req.headers and "te" not in req.headers
    assert str(req.url) == gw.UPSTREAM_BASE + VIDEO


def test_tope_de_request_413_declarado_y_en_streaming(gw, monkeypatch):
    assert gw.MAX_BODY_BYTES == 32 * 1024 * 1024
    monkeypatch.setattr(gw, "MAX_BODY_BYTES", 10)
    up = Upstream(lambda r, n: reply_json(200, {}))
    assert call(gw, up, "POST", MEDIA, content=b"x" * 11).status_code == 413

    async def troceado():
        for _ in range(3):
            yield b"abcd"

    assert call(gw, up, "POST", MEDIA, content=troceado()).status_code == 413  # sin Content-Length
    assert up.seen == []


def test_semaforo_de_8_llamadas_simultaneas_por_replica(gw, monkeypatch):
    assert gw.MAX_CONCURRENT == 8
    monkeypatch.setattr(gw, "_sem", asyncio.Semaphore(2))
    activas = {"now": 0, "max": 0}

    async def upstream(request):
        activas["now"] += 1
        activas["max"] = max(activas["max"], activas["now"])
        await asyncio.sleep(0.02)
        activas["now"] -= 1
        return reply_json(200, {})

    async def run():
        gw._client = None
        gw._transport = httpx.MockTransport(upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw") as c:
            return await asyncio.gather(*[c.post(MEDIA, headers=STUDIO, content=b"{}") for _ in range(6)])

    assert all(r.status_code == 200 for r in asyncio.run(run()))
    assert activas["max"] == 2


# --------------------------------------------------------------------------- C4 failover


def test_c4_429_un_reintento_a_la_otra_cuenta(gw, capsys):
    up = Upstream(lambda r, n: reply_json(429, {"code": "Throttling.RateQuota"}) if n == 1
                  else reply_json(200, {"output": "ok"}))
    r = call(gw, up, "POST", MEDIA, content=b'{"model":"wan2.7-image"}')
    assert r.status_code == 200 and r.json() == {"output": "ok"}
    assert r.headers["x-plan-account"] == "k2"
    assert up.accounts == ["k1", "k2"]
    rows = ledger(capsys)
    assert [(x["attempt"], x["account"], x["status"], x["quota_error"]) for x in rows] == [
        (1, "k1", 429, True), (2, "k2", 200, False)]


def test_c4_200_con_cuota_agotada_en_el_body_tambien_reintenta(gw, capsys):
    cuerpos = [{"code": "Arrearage", "message": "x"}, {"error": {"code": "insufficient_quota"}}]
    for cuerpo in cuerpos:
        up = Upstream(lambda r, n, c=cuerpo: reply_json(200, c) if n == 1 else reply_json(200, {"ok": 1}))
        r = call(gw, up, "POST", TTS)
        assert r.json() == {"ok": 1} and r.headers["x-plan-account"] == "k2" and up.accounts == ["k1", "k2"]
    assert [x["quota_error"] for x in ledger(capsys)] == [True, False, True, False]


def test_c4_el_sorteo_manda_la_primera_cuenta(gw):
    gw.draw_first = "k2"
    up = Upstream(lambda r, n: reply_json(429, {}) if n == 1 else reply_json(200, {}))
    assert call(gw, up, "POST", MEDIA).headers["x-plan-account"] == "k1"
    assert up.accounts == ["k2", "k1"]


def test_c4_error_de_conexion_antes_de_enviar_reintenta(gw, capsys):
    up = Upstream(lambda r, n: httpx.ConnectError("sin ruta") if n == 1 else reply_json(200, {"ok": 1}))
    r = call(gw, up, "POST", VIDEO)
    assert r.status_code == 200 and r.headers["x-plan-account"] == "k2" and up.accounts == ["k1", "k2"]
    rows = ledger(capsys)
    assert rows[0]["status"] is None and rows[0]["error"] == "ConnectError" and rows[1]["status"] == 200


def test_c4_read_timeout_en_post_no_reintenta(gw, capsys):
    up = Upstream(lambda r, n: httpx.ReadTimeout("lento"))
    r = call(gw, up, "POST", VIDEO)
    assert r.status_code == 504 and r.headers["x-plan-account"] == "k1"
    assert up.accounts == ["k1"], "la tarea pudo crearse y cobrarse: nunca un segundo POST"
    assert len(ledger(capsys)) == 1


def test_c4_fallan_las_dos_se_devuelve_la_ultima_respuesta_tal_cual(gw):
    ultimo = {"code": "Throttling.AllocationQuota", "message": "segunda", "request_id": "r2"}
    up = Upstream(lambda r, n: reply_json(429, {"code": "Throttling", "message": "primera"}) if n == 1
                  else reply_json(429, ultimo, headers={"x-extra": "2"}))
    r = call(gw, up, "POST", MEDIA)
    assert r.status_code == 429 and r.json() == ultimo and r.headers["x-extra"] == "2"
    assert r.headers["x-plan-account"] == "k2" and up.accounts == ["k1", "k2"]


def test_c4_si_la_segunda_ni_responde_sale_la_ultima_respuesta_real(gw):
    up = Upstream(lambda r, n: reply_json(429, {"code": "Throttling"}) if n == 1 else httpx.ConnectError("x"))
    r = call(gw, up, "POST", MEDIA)
    assert r.status_code == 429 and r.json() == {"code": "Throttling"} and r.headers["x-plan-account"] == "k1"


def test_c4_dos_conexiones_caidas_502_visible(gw):
    up = Upstream(lambda r, n: httpx.ConnectError("x"))
    r = call(gw, up, "POST", MEDIA)
    assert r.status_code == 502 and r.json() == {"error": "upstream_unreachable"} and up.accounts == ["k1", "k2"]


def test_c4_un_5xx_o_un_400_normal_no_se_reintenta(gw):
    for status in (400, 500, 503):
        up = Upstream(lambda r, n, s=status: reply_json(s, {"code": "InvalidParameter"}))
        assert call(gw, up, "POST", MEDIA).status_code == status and up.accounts == ["k1"]


def test_c4_respuesta_no_json_va_en_streaming_sin_bufferizar_ni_reintentar(gw):
    wav = [b"RIFF", b"....", b"WAVE"]
    up = Upstream(lambda r, n: reply(200, ctype="audio/wav", chunks=wav, headers={"x-request-id": "q"}))
    r = call(gw, up, "POST", TTS)
    assert r.content == b"".join(wav) and r.headers["content-type"] == "audio/wav"
    assert r.headers["x-request-id"] == "q" and r.headers["x-plan-account"] == "k1" and up.accounts == ["k1"]


def test_c4_una_cuenta_sin_key_no_se_sortea_ni_se_reintenta_en_ella(gw, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY_2")

    async def draw(accounts):
        assert accounts == ["k1"]
        return "k1"

    monkeypatch.setattr(gw.session_router, "draw_account", draw)
    up = Upstream(lambda r, n: reply_json(429, {}))
    assert call(gw, up, "POST", MEDIA).status_code == 429 and up.accounts == ["k1"]
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    assert call(gw, up, "POST", MEDIA).status_code == 503


def test_el_sorteo_real_de_session_router_reparte_por_los_pesos(gw, monkeypatch):
    """Sin el parche de `draw_account`: la formula del proxy, con pesos del panel."""
    monkeypatch.setattr(gw.session_router, "draw_account", gw.real_draw)

    async def pesos():
        return {"k1": 0.0, "k2": 1.0}

    monkeypatch.setattr(gw.session_router, "_account_weights", pesos)
    up = Upstream(lambda r, n: reply_json(200, {}))
    for _ in range(12):
        assert call(gw, up, "POST", MEDIA).headers["x-plan-account"] == "k2"


# --------------------------------------------------------------------------- tareas sin estado


def test_tarea_con_cabecera_va_a_esa_cuenta_y_solo_a_esa(gw):
    up = Upstream(lambda r, n: reply_json(404, {"code": "InvalidParameter"}) if n == 1 else reply_json(200, {}))
    r = call(gw, up, "GET", "/api/v1/tasks/abc-123", headers={**STUDIO, "X-Plan-Account": "k2"})
    assert up.accounts == ["k2"] and r.status_code == 404 and r.headers["x-plan-account"] == "k2"
    assert str(up.seen[0].url) == gw.UPSTREAM_BASE + "/api/v1/tasks/abc-123"
    assert call(gw, up, "GET", "/api/v1/tasks/abc", headers={**STUDIO, "X-Plan-Account": "k9"}).status_code == 400


def test_tarea_sin_cabecera_prueba_las_dos_y_devuelve_la_que_la_conoce(gw, capsys):
    gw.draw_first = "k2"
    up = Upstream(lambda r, n: reply_json(404, {"code": "InvalidParameter", "message": "task not found"})
                  if n == 1 else reply_json(200, {"output": {"task_status": "RUNNING"}}))
    r = call(gw, up, "GET", "/api/v1/tasks/abc")
    assert r.status_code == 200 and r.headers["x-plan-account"] == "k1"
    assert up.accounts == ["k2", "k1"]          # orden del sorteo
    assert [x["attempt"] for x in ledger(capsys)] == [1, 2]
    up = Upstream(lambda r, n: reply_json(200, {"ok": 1}))
    assert call(gw, up, "GET", "/api/v1/tasks/abc").headers["x-plan-account"] == "k2" and up.accounts == ["k2"]
    # el 404 de «tarea desconocida» puede no ser JSON: tambien prueba la otra cuenta
    up = Upstream(lambda r, n: reply(404, b"not found", ctype="text/plain") if n == 1 else reply_json(200, {}))
    r = call(gw, up, "GET", "/api/v1/tasks/abc")
    assert r.status_code == 200 and up.accounts == ["k2", "k1"]
    # ...y si la segunda ni responde, sale la respuesta real de la primera (bufferizada)
    up = Upstream(lambda r, n: reply(404, b"not found", ctype="text/plain") if n == 1 else httpx.ConnectError("x"))
    r = call(gw, up, "GET", "/api/v1/tasks/abc")
    assert r.status_code == 404 and r.content == b"not found" and r.headers["x-plan-account"] == "k2"


# --------------------------------------------------------------------------- C5 ledger


def test_c5_ledger_una_linea_json_por_intento_con_todos_los_campos(gw, capsys):
    up = Upstream(lambda r, n: reply_json(429, {}) if n == 1 else reply_json(200, {}))
    call(gw, up, "POST", VIDEO, content=b'{"model": "happyhorse-1.1-t2v", "input": {}}')
    out = capsys.readouterr().out
    lineas = [l for l in out.splitlines() if l]
    assert len(lineas) == 2 and all(l.startswith("{") for l in lineas)
    rows = [json.loads(l) for l in lineas]
    for row in rows:
        assert list(row) == ["ts", "caller", "method", "path", "model", "attempt", "account",
                             "status", "quota_error", "ms"]
        assert row["caller"] == "studio" and row["method"] == "POST" and row["path"] == VIDEO
        assert row["model"] == "happyhorse-1.1-t2v" and isinstance(row["ms"], int)
    assert [r["attempt"] for r in rows] == [1, 2] and [r["account"] for r in rows] == ["k1", "k2"]
    assert "sk-k1" not in out and "tok-studio" not in out


# --------------------------------------------------------------------------- lifespan


def test_lifespan_calienta_la_config_antes_de_aceptar_trafico(gw, monkeypatch):
    orden = []

    async def warm():
        orden.append("warm")

    monkeypatch.setattr(gw.session_router, "warm_config", warm)
    mensajes = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive():
        return next(mensajes)

    async def send(message):
        orden.append(message["type"])

    asyncio.run(gw.app({"type": "lifespan"}, receive, send))
    assert orden == ["warm", "lifespan.startup.complete", "lifespan.shutdown.complete"]
