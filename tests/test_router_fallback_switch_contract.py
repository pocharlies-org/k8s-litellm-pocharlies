"""INFRA-1243: el interruptor «Fallback del Router» (tarjeta ALIBABA · TOKEN PLAN de /inferencia)
corta solo las aristas hacia Alibaba. Apagado, el hook estampa por petición el grafo de
router_settings.fallbacks sin los destinos `alibaba-*`: las aristas a mac-casa (`*-mac`) siguen,
porque son hardware propio y no sacan nada de casa."""
import ast
import types

import yaml

from test_aurora_fallback_policy_contract import MANIFEST, _hook_source


def _router_fallbacks():
    config = next(
        yaml.safe_load(doc["data"]["config.yaml"])
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config"
    )
    return config["router_settings"]["fallbacks"]


def _sin_alibaba():
    fn = next(
        node for node in ast.parse(_hook_source()).body
        if isinstance(node, ast.FunctionDef) and node.name == "_fallbacks_sin_alibaba"
    )
    module = types.ModuleType("router_fallback_switch")
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<hook>", "exec"), module.__dict__)
    return module._fallbacks_sin_alibaba


def test_apagado_quita_solo_los_destinos_alibaba():
    grafo = _router_fallbacks()
    estampado = _sin_alibaba()(grafo)

    destinos = [t for edge in estampado for ts in edge.values() for t in ts]
    assert destinos and not any(t.startswith("alibaba-") for t in destinos)
    # Todo lo que no iba a Alibaba sigue tal cual, en el mismo orden.
    assert estampado == [
        {g: [t for t in ts if not t.startswith("alibaba-")]}
        for edge in grafo for g, ts in edge.items()
        if any(not t.startswith("alibaba-") for t in ts)
    ]


def test_las_aristas_de_mac_casa_sobreviven_y_las_del_residente_no():
    estampado = {g: ts for edge in _sin_alibaba()(_router_fallbacks()) for g, ts in edge.items()}

    assert estampado["stt-turbo"] == ["stt-turbo-mac"]
    assert estampado["whisper-1"] == ["stt-turbo-mac"]
    assert estampado["bge-m3"] == estampado["bge-m3-embedding"] == ["bge-m3-mac"]
    assert estampado["bge-reranker"] == estampado["bge-reranker-v2-m3"] == ["bge-reranker-mac"]
    assert "qwen38-flash-next" not in estampado
    assert "correo-clasificador" not in estampado


def test_sin_grafo_estampa_lista_vacia():
    fn = _sin_alibaba()
    assert fn(None) == [] and fn([]) == []
    assert fn([{"x": ["alibaba-q38-flash"]}]) == []
