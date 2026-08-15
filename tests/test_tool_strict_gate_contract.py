"""Contrato del gate de args de tool (2026-07-27).

vLLM SALTA la gramatica del schema cuando la peticion usa `tool_choice: "auto"`
y ninguna tool declara `strict` (vllm/tool_parsers/structural_tag_registry.py:
`if tool_choice == "auto" and not _any_tool_strict(tools): return None`). Como
`auto` es justo lo que mandan los agentes, el hook marca las tools de NUESTROS
backends para que xgrammar restrinja la decodificacion y una violacion de
enum/tipo sea imposible en generacion.

Medido antes de implementarlo, contra el caso real negative-invalidarg-000 en el
27B uncensored: sin strict mode='write' 3/3 (viola el enum de
[audit, dry_run, read_only]); con strict mode='read_only' 3/3.
"""
import ast
import logging
import os
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


@pytest.fixture(scope="module")
def gate():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(d["data"]["litellm_strip_params.py"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_enforce_tool_strict")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "STRICT_GATE_ENABLED" for t in n.targets))]
    assert any(isinstance(n, ast.FunctionDef) for n in keep), "el hook ya no define _enforce_tool_strict"
    mod = types.ModuleType("gatepure")
    mod.__dict__.update({"os": os, "tool_gate_log": logging.getLogger("test")})
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<gate>", "exec"), mod.__dict__)
    return mod


def _tool(**extra):
    fn = {"name": "f", "parameters": {"type": "object",
                                      "properties": {"mode": {"enum": ["a", "b"]}}}}
    fn.update(extra)
    return {"type": "function", "function": fn}


def test_marks_plain_tools(gate):
    data = {"tools": [_tool(), _tool()]}
    assert gate._enforce_tool_strict(data, "m") == 2
    assert all(t["function"]["strict"] is True for t in data["tools"])


@pytest.mark.parametrize("explicit", [True, False])
def test_client_intent_wins(gate, explicit):
    """Un `strict` explicito del cliente NO se pisa, ni siquiera si es False."""
    data = {"tools": [_tool(strict=explicit)]}
    assert gate._enforce_tool_strict(data, "m") == 0
    assert data["tools"][0]["function"]["strict"] is explicit


def test_noop_without_schema_or_tools(gate):
    # sin properties no hay gramatica que aplicar
    data = {"tools": [{"type": "function",
                       "function": {"name": "f", "parameters": {"type": "object"}}}]}
    assert gate._enforce_tool_strict(data, "m") == 0
    assert "strict" not in data["tools"][0]["function"]
    # y sin tools, o con tools mal formadas, no explota
    assert gate._enforce_tool_strict({"messages": []}, "m") == 0
    assert gate._enforce_tool_strict({"tools": "nope"}, "m") == 0
    assert gate._enforce_tool_strict({"tools": [None, 3, "x"]}, "m") == 0


def test_gate_is_wired_into_local_vllm_requests():
    """Debe llamarse SOLO para nuestros backends (dentro del bloque
    _is_local_vllm_request), nunca para modelos de terceros.

    2026-08-11: la guarda lleva ademas `not vision_diverted`. Una peticion con
    imagen desviada a ChatGPT sigue teniendo proxy_model=`tooling`, asi que sin
    esa condicion _is_local_vllm_request diria True y se le marcarian las tools
    como strict para una gramatica de vLLM que no la va a servir.
    """
    text = MANIFEST.read_text()
    block = text[text.index(
        "if not vision_diverted and _is_local_vllm_request(model, proxy_model, api_base):"):]
    block = block[:block.index("tracking_id = str(uuid.uuid4())")]
    assert "_enforce_tool_strict(data" in block


def test_hosted_tooling_fallback_bypasses_local_admission():
    """El modelo ya resuelto manda sobre el alias original de la peticion.

    Cuando `tooling` cae a Terra, `proxy_model` sigue diciendo `tooling`. La
    deteccion debe tratar los dos destinos hosted como externos para que el gate
    de compute-mode no convierta el fallback valido en un 503.
    """
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(d["data"]["litellm_strip_params.py"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_is_local_vllm_request")
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "HOSTED_MODEL_PREFIXES"
                        for t in n.targets))]
    mod = types.ModuleType("localrequestpure")
    mod.__dict__.update({
        "LOCAL_VLLM_ALIASES": {"tooling"},
        "resolve_server_id": lambda model, api_base: None,
    })
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<local-request>", "exec"),
         mod.__dict__)

    assert mod._is_local_vllm_request("tooling", "tooling", "")
    assert not mod._is_local_vllm_request(
        "cloudblue/gpt-5.6-terra", "tooling", ""
    )
    assert not mod._is_local_vllm_request(
        "e-dani/gpt-5.6-terra", "tooling", ""
    )


def test_gate_has_an_env_kill_switch():
    text = MANIFEST.read_text()
    assert 'LITELLM_TOOL_STRICT_GATE' in text
