"""Config de routing por el stream SSE del dashboard (DGX-1099, contrato dgx.model-routing.config.stream.v1):
los `event: config` llenan la caché, con el stream vivo no hay GET, al caer vuelve el GET con TTL, y
MODEL_ROUTING_CONFIG_STREAM=off es el sondeo de siempre."""
import asyncio
import json
import sys
import types

from test_alibaba_account_affinity import ROUTER_SRC


class _Resp:
    def __init__(self, lineas):
        self.lineas = lineas

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for linea in self.lineas:
            yield linea


class _Cliente:
    def __init__(self, lineas, abiertas):
        self.lineas, self.abiertas = lineas, abiertas

    is_closed = False

    def stream(self, metodo, url, **kw):
        self.abiertas.append(url)
        cliente = self

        class _Ctx:
            async def __aenter__(self):
                return _Resp(cliente.lineas)

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _mod(lineas=(), stream="on"):
    abiertas = []
    httpx = types.ModuleType("httpx")
    creados = []
    httpx.AsyncClient = lambda **kw: creados.append(1) or _Cliente(list(lineas), abiertas)
    httpx.Timeout = lambda *a, **kw: None
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = httpx
    import os
    os.environ["MODEL_ROUTING_CONFIG_STREAM"] = stream
    try:
        mod = types.ModuleType("session_router_stream")
        exec(compile(ROUTER_SRC, "session_router.py", "exec"), mod.__dict__)
    finally:
        os.environ.pop("MODEL_ROUTING_CONFIG_STREAM", None)
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)
    mod._creados = creados
    return mod, abiertas


def test_los_eventos_config_llenan_la_cache_y_los_latidos_no():
    lineas = [": ping", "", "event: config", "id: 1", 'data: {"sticky": true}', "",
              "event: otro", 'data: {"sticky": false}', "",
              "event: config", 'data: {"sticky": false, "local_slots": 3}', ""]
    mod, abiertas = _mod(lineas)
    vistos = []
    real = mod._sanitize
    mod._sanitize = lambda c: vistos.append(c) or real(c)

    async def caso():
        async def corta(_s):
            raise asyncio.CancelledError
        mod.asyncio = types.SimpleNamespace(**{k: getattr(asyncio, k) for k in dir(asyncio) if not k.startswith("_")})
        mod.asyncio.sleep = corta
        try:
            await mod._config_stream()
        except asyncio.CancelledError:
            pass
    asyncio.run(caso())
    assert len(mod._creados) == 1, "un cliente para todas las reconexiones"
    assert abiertas == [mod.CONFIG_URL + "/stream"]
    assert vistos == [{"sticky": True}, {"sticky": False, "local_slots": 3}]
    assert mod._config_cache["config"]["local_slots"] == 3
    assert mod._config_stream_live[0] is False, "al acabarse el stream, vuelve el GET"


def test_con_el_stream_vivo_no_hay_get_y_sin_el_si():
    mod, _ = _mod(stream="off")
    gets = []

    async def refresh():
        gets.append(1)
    mod._refresh_config = refresh

    async def caso():
        mod._config_cache["expires"] = 0.0
        mod._config_stream_live[0] = True
        await mod._config()
        await asyncio.sleep(0)
        assert gets == []
        mod._config_stream_live[0] = False
        await mod._config()
        await asyncio.sleep(0)
        assert gets == [1]
    asyncio.run(caso())


def test_off_no_abre_el_stream():
    mod, abiertas = _mod(stream="off")

    async def refresh():
        return None
    mod._refresh_config = refresh
    asyncio.run(mod._config())
    assert mod._config_stream_task is None and abiertas == []
