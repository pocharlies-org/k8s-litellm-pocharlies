"""Contrato: stream_options jamas sale hacia un backend sin stream (SC-1517).

POR QUE EXISTE (01-10-2026)
---------------------------
Los turnos de Claude Code contra `qwen38-flash-next` con la tool de busqueda
web activa morian con 400 "Stream options can only be defined when
stream=True" (vLLM) y, en el fallback, "'stream' and 'stream_options' must be
set together" (DashScope) -> "No fallback model group found". Medido contra el
proxy vivo, la cadena es:

  1. El hook (`async_pre_call_hook`, stream=True, /v1/messages) escribe
     `stream_options` en el top-level y en `extra_body` (via_extra_body=True,
     unico camino que sobrevive al adaptador y da uso continuo al panel).
  2. `websearch_interception` (enabled_providers: openai) flipa `stream`
     True -> False DENTRO de litellm.anthropic_messages cuando la peticion
     trae la tool de busqueda nativa, y no toca lo que hay en extra_body.
  3. El adaptador Anthropic->OpenAI de la v1.100 reenvia `extra_body` tal cual
     (el bucle de `_create_completion_kwargs` lo copia si no esta ya en
     completion_kwargs) y el SDK de OpenAI lo FUNDE dentro del body final:
     stream_options sin stream -> 400 en el hop primario y en cada reintento
     y salto de fallback, que reentran con las mismas kwargs.

Las tres guardas del fix se prueban aqui, mas la condicion de que el guard de
pre-request este registrado DESPUES de websearch_interception (el orden de
`litellm_settings.callbacks` es lo unico que hace que vea el stream ya flipado).
"""
import ast
import asyncio
import types

import pytest
import yaml

from manifest_docs import config_data as _configmap


def _extract(src, names):
    """Carga las funciones nombradas del codigo embebido sin ejecutar el resto."""
    tree = ast.parse(src)
    keep = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in names
    ]
    assert len(keep) == len(names), f"faltan funciones: {names}"
    mod = types.ModuleType("pure")
    mod.__dict__["log"] = __import__("logging").getLogger("test")
    # Los ficheros embebidos arrancan con `from __future__ import annotations`;
    # sin ese flag las anotaciones (`Dict[str, Any]`) se evaluan al def y petan.
    import __future__

    code = compile(
        ast.Module(body=keep, type_ignores=[]),
        "<pure>",
        "exec",
        flags=__future__.annotations.compiler_flag,
    )
    exec(code, mod.__dict__)
    return mod


@pytest.fixture(scope="module")
def strip():
    return _extract(
        _configmap("litellm-config")["litellm_strip_params.py"],
        ["_has_web_search_tool", "_strip_stream_options_without_stream"],
    )


@pytest.fixture(scope="module")
def tracking():
    return _extract(
        _configmap("litellm-config")["active_request_tracking.py"],
        ["enable_continuous_usage"],
    )


# ── enable_continuous_usage: sin codigo muerto tras el return ──────────────


def test_noop_sin_stream_y_sin_codigo_muerto(tracking):
    # El codigo muerto que quedaba tras `return data` escribia stream_options
    # en peticiones no-stream si alguien movia el return; el test de abajo lo
    # pilla por el lado del comportamiento: no-stream NO debe quedar con
    # stream_options por ninguna via.
    for stream in (None, False, "true"):
        data = {"extra_body": {"cache_salt": "refusal:0"}}
        if stream is not None:
            data["stream"] = stream
        out = tracking.enable_continuous_usage(data, via_extra_body=True)
        assert "stream_options" not in out
        assert "stream_options" not in out["extra_body"]


def test_stream_true_escribe_top_level_y_extra_body(tracking):
    data = {"stream": True, "extra_body": {"cache_salt": "refusal:0"}}
    out = tracking.enable_continuous_usage(data, via_extra_body=True)
    assert out["stream_options"] == {"include_usage": True, "continuous_usage_stats": True}
    assert out["extra_body"]["stream_options"] == out["stream_options"]
    assert out["extra_body"]["cache_salt"] == "refusal:0"


# ── _has_web_search_tool: la condicion del flip ────────────────────────────


def test_detecta_server_tool_nativa(strip):
    assert strip._has_web_search_tool(
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]}
    ) is True


def test_detecta_tool_convertida_y_function_wrapper(strip):
    assert strip._has_web_search_tool({"tools": [{"name": "litellm_web_search"}]}) is True
    assert strip._has_web_search_tool(
        {"tools": [{"type": "function", "function": {"name": "web_search"}}]}
    ) is True


def test_no_falsos_positivos(strip):
    assert strip._has_web_search_tool({"tools": [{"name": "Bash"}]}) is False
    assert strip._has_web_search_tool({}) is False
    assert strip._has_web_search_tool({"tools": [None, "raro"]}) is False


# ── _strip_stream_options_without_stream: corte en el pre-call ─────────────


def test_los_dos_caminos_quedan_sin_restos_sin_stream(strip):
    data = {
        "stream_options": {"include_usage": True},
        "extra_body": {"stream_options": {"include_usage": True}, "cache_salt": "x"},
    }
    strip._strip_stream_options_without_stream(data)
    assert "stream_options" not in data
    assert "stream_options" not in data["extra_body"]
    assert data["extra_body"]["cache_salt"] == "x"  # el sello no se toca


def test_con_stream_true_no_toca_nada(strip):
    so = {"include_usage": True, "continuous_usage_stats": True}
    data = {"stream": True, "stream_options": dict(so), "extra_body": {"stream_options": dict(so)}}
    strip._strip_stream_options_without_stream(data)
    assert data["stream_options"] == so
    assert data["extra_body"]["stream_options"] == so


def test_stream_false_quita(strip):
    data = {"stream": False, "stream_options": {"include_usage": True}}
    strip._strip_stream_options_without_stream(data)
    assert "stream_options" not in data


# ── el guard de pre-request (el flip de websearch_interception) ────────────


@pytest.fixture(scope="module")
def guard():
    src = _configmap("litellm-config")["litellm_strip_params.py"]
    tree = ast.parse(src)
    cls = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "StreamOptionsWithoutStreamGuard"
    )
    mod = types.ModuleType("guard")
    litellm_stub = types.SimpleNamespace(
        integrations=types.SimpleNamespace(
            custom_logger=types.SimpleNamespace(CustomLogger=object)
        )
    )
    mod.__dict__["litellm"] = litellm_stub
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "<guard>", "exec"), mod.__dict__)
    return mod.StreamOptionsWithoutStreamGuard()


def test_guard_quita_restos_tras_el_flip(guard):
    kwargs = {
        "stream": False,
        "_websearch_interception_converted_stream": True,
        "stream_options": {"include_usage": True},
        "extra_body": {"stream_options": {"continuous_usage_stats": True}, "cache_salt": "x"},
    }
    out = asyncio.run(guard.async_pre_request_hook("m", [], kwargs))
    assert out is kwargs
    assert "stream_options" not in out
    assert "stream_options" not in out["extra_body"]
    assert out["extra_body"]["cache_salt"] == "x"


def test_guard_no_op_con_stream_o_sin_restos(guard):
    kwargs = {"stream": True, "stream_options": {"include_usage": True}}
    assert asyncio.run(guard.async_pre_request_hook("m", [], kwargs)) is None
    kwargs = {"stream": False}
    assert asyncio.run(guard.async_pre_request_hook("m", [], kwargs)) is None


# ── cableado: el guard tiene que correr DESPUES de quien flipa stream ──────


def test_guard_registrado_tras_websearch_interception():
    config = yaml.safe_load(_configmap("litellm-config")["config.yaml"])
    callbacks = config["litellm_settings"]["callbacks"]
    assert "litellm_strip_params.stream_guard_instance" in callbacks
    assert callbacks.index("litellm_strip_params.stream_guard_instance") > callbacks.index(
        "websearch_interception"
    ), "el guard debe ver el stream ya flipado: va despues de websearch_interception"
