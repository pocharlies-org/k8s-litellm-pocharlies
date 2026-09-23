"""Caché de prefijos del residente (23-09-2026).

Dos causas medidas de prefill en frío en el residente:
* la línea de facturación de Claude Code (`x-anthropic-billing-header: cc_version=
  <v>.<3 hex>`), primer bloque del system y distinta en cada conversación: impedía
  compartir system+tools entre subagentes/sesiones;
* el estado del compute-mode en None cuando el dashboard no contesta: 503 a todo
  lo local y sesiones calientes mudadas a Alibaba (rebind_alibaba).
"""
import ast
import asyncio
import types
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _hook_source():
    return next(
        doc["data"]["litellm_strip_params.py"]
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config"
    )


def _load(names, extra_globals=None):
    src = _hook_source()
    tree = ast.parse(src)
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in names for t in node.targets
        ):
            keep.append(node)
    mod = types.ModuleType("hook_part")
    mod.__dict__.update(extra_globals or {})
    exec(compile(ast.Module(body=keep, type_ignores=[]), "hook_part", "exec"), mod.__dict__)
    return mod


BILLING = {"_strip_billing_header", "_strip_billing_line", "_is_billing_block", "BILLING_HEADER_PREFIX"}


def test_quita_el_bloque_de_facturacion_del_system_anthropic():
    m = _load(BILLING)
    data = {"system": [
        {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.280.2be; cc_entrypoint=sdk-cli;"},
        {"type": "text", "text": "You are a Claude agent", "cache_control": {"type": "ephemeral"}},
    ]}
    assert m._strip_billing_header(data) is True
    assert data["system"] == [{"type": "text", "text": "You are a Claude agent", "cache_control": {"type": "ephemeral"}}]


def test_dos_conversaciones_quedan_con_el_mismo_system():
    m = _load(BILLING)
    a = {"system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.280.2be;"},
                    {"type": "text", "text": "SISTEMA"}]}
    b = {"system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.280.b58;"},
                    {"type": "text", "text": "SISTEMA"}]}
    m._strip_billing_header(a)
    m._strip_billing_header(b)
    assert a == b


def test_system_str_y_mensaje_openai():
    m = _load(BILLING)
    data = {"system": "x-anthropic-billing-header: cc_version=1.x;\nresto del sistema",
            "messages": [
                {"role": "system", "content": "x-anthropic-billing-header: v;\nsistema openai"},
                {"role": "system", "content": [{"type": "text", "text": "x-anthropic-billing-header: v;"},
                                               {"type": "text", "text": "parte"}]},
                {"role": "user", "content": "x-anthropic-billing-header: esto lo escribio el usuario"},
            ]}
    assert m._strip_billing_header(data) is True
    assert data["system"] == "resto del sistema"
    assert data["messages"][0]["content"] == "sistema openai"
    assert data["messages"][1]["content"] == [{"type": "text", "text": "parte"}]
    # los mensajes de usuario no se tocan
    assert data["messages"][2]["content"].startswith("x-anthropic-billing-header")


def test_sin_cabecera_no_toca_nada():
    m = _load(BILLING)
    data = {"system": [{"type": "text", "text": "hola"}], "messages": [{"role": "user", "content": "x"}]}
    antes = repr(data)
    assert m._strip_billing_header(data) is False
    assert repr(data) == antes
    assert m._strip_billing_header({}) is False


def test_el_hook_la_quita_lo_primero():
    src = _hook_source()
    i = src.index("async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):")
    j = src.index("_strip_billing_header(data)", i)
    k = src.index('requested_model = data.get("model", "")', i)
    assert j < k


class _Log:
    def __init__(self):
        self.errors = []

    def error(self, *a):
        self.errors.append(a)


def _compute_mode(responses):
    """_compute_mode_state con httpx falso que devuelve/lanza según `responses`."""
    it = iter(responses)

    class Resp:
        def __init__(self, v):
            self.v = v

        def raise_for_status(self):
            if isinstance(self.v, Exception):
                raise self.v

        def json(self):
            return self.v

    class Cli:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return Resp(next(it))

    clock = {"t": 1000.0}
    fake_time = types.SimpleNamespace(monotonic=lambda: clock["t"])
    m = _load({"_compute_mode_state", "_compute_mode_cache", "COMPUTE_MODE_STALE_OK_SECONDS"}, {
        "httpx": types.SimpleNamespace(AsyncClient=Cli),
        "time": fake_time,
        "asyncio": asyncio,
        "os": __import__("os"),
        "log": _Log(),
        "COMPUTE_MODE_URL": "http://x",
        "COMPUTE_MODE_TIMEOUT_SECONDS": 1,
        "COMPUTE_MODE_CACHE_SECONDS": 5,
    })
    m._compute_mode_lock = asyncio.Lock()
    return m, clock


def test_compute_mode_usa_el_ultimo_bueno_si_el_dashboard_no_contesta():
    bueno = {"phase": "ready", "effective_mode": "llm-tp"}
    m, clock = _compute_mode([bueno, RuntimeError("All connection attempts failed")])
    assert asyncio.run(m._compute_mode_state()) == bueno
    clock["t"] += 6                                     # caduca la caché de 5 s
    assert asyncio.run(m._compute_mode_state()) == bueno  # fallo -> último bueno


def test_compute_mode_ultimo_bueno_caduca():
    bueno = {"phase": "ready"}
    m, clock = _compute_mode([bueno, RuntimeError("caido")])
    asyncio.run(m._compute_mode_state())
    clock["t"] += m.COMPUTE_MODE_STALE_OK_SECONDS + 1
    assert asyncio.run(m._compute_mode_state()) is None   # pasado el margen, fail-closed


def test_compute_mode_sin_lectura_buena_previa_es_none():
    m, clock = _compute_mode([RuntimeError("caido")])
    assert asyncio.run(m._compute_mode_state()) is None
