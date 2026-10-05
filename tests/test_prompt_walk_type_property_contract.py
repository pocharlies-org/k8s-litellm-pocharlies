"""DGX-584 (2026-10-05): recorrer el body no puede reventar con un JSON-Schema.

`prompt_chars()` (active_request_tracking.py) y `_estimate_prompt_tokens()`
(session_router.py) recorren el body buscando bloques de medio por su `type`.
Un tool con una PROPIEDAD que se llama `type` — el tool `Artifact` de Claude
Code trae `input_schema.properties.type = {"description": …}` — mete un DICT en
ese hueco, y `dict in frozenset` es `TypeError: unhashable type: 'dict'`.

El crash no se veia: en el hook va dentro del `try/except` que estampa el
request, que lo traga con un `log.debug`. El efecto era que
`request_tracker.start()` no llegaba a ejecutarse nunca en los turnos de Claude
Code y la tarjeta del motor se quedaba sin filas en vuelo (el panel de requests
en vivo del residente, vacío con el motor trabajando). En session_router el
mismo crash caia en su `except` fail-open: la valvula de presupuesto KV se
quedaba ciega justo con los turnos grandes.

Misma tecnica que tests/test_priority_injection_contract.py: los modulos se
cargan del EMBED del manifest, que es la fuente de verdad.
"""
import ast
import sys
import types

import yaml
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _configmap():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    return next(d for d in docs
                if d.get("kind") == "ConfigMap"
                and d["metadata"]["name"] == "litellm-config")


def _module(source, name):
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    exec(compile(source, name, "exec"), mod.__dict__)
    return mod


def _funcs(source, names):
    """Solo las funciones pedidas (y las constantes de modulo) del EMBED.

    `litellm_strip_params.py` no se puede ejecutar entero fuera del pod: importa
    el paquete `litellm` y sus handlers. Los dos ayudantes que se comprueban aqui
    no los usan, asi que se recortan del AST.
    """
    tree = ast.parse(source)

    def stdlib_import(node):
        return (isinstance(node, ast.Import)
                and all(a.name.split(".")[0] in sys.stdlib_module_names
                        for a in node.names))

    keep = [n for n in tree.body
            if stdlib_import(n)
            or (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name in names)]
    mod = types.ModuleType("strip_funcs")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "strip_funcs", "exec"),
         mod.__dict__)
    return mod


TRACKER = _module(_configmap()["data"]["active_request_tracking.py"],
                  "active_request_tracking_type_property")
STRIP = _funcs(_configmap()["data"]["litellm_strip_params.py"],
               {"_has_part_type", "_message_entries", "_is_structured_output"})
ROUTER = _module(_configmap()["data"]["session_router.py"],
                 "session_router_type_property")


def _body_con_propiedad_type():
    """Turno tipo Claude Code: un tool cuyo schema declara una propiedad `type`."""
    return {
        "model": "qwen38-flash-next",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "Eres un agente."}],
        "messages": [{"role": "user", "content": "Lista los artefactos."}],
        "tools": [{
            "name": "Artifact",
            "description": "Publica una pagina",
            "input_schema": {
                "type": "object",
                "properties": {
                    "type": {
                        "description": "list only: the name of a published "
                                       "Artifact type",
                        "type": "string",
                    },
                    "url": {"type": "string"},
                },
                "required": ["type"],
            },
        }],
    }


def test_prompt_chars_no_reventa_con_una_propiedad_llamada_type():
    total, has_media = TRACKER.prompt_chars(_body_con_propiedad_type())
    assert has_media is False, "una propiedad `type` no es un bloque de medio"
    assert total > 0, "el texto del prompt se sigue contando"


def test_prompt_chars_sigue_detectando_el_block_de_medio():
    total, has_media = TRACKER.prompt_chars({
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "que hay en la foto"},
            {"type": "image", "source": {"type": "base64", "data": "AAA"}},
        ]}],
    })
    assert has_media is True
    assert total > 0


def test_estimate_prompt_tokens_no_se_queda_ciego_con_esas_tools():
    """Fail-open silencioso: None = la valvula KV no decide nada."""
    estim = ROUTER._estimate_prompt_tokens(_body_con_propiedad_type())
    assert isinstance(estim, int) and estim > 0, (
        "con tools que declaran una propiedad `type` la valvula se quedaba "
        f"sin presupuesto: {estim!r}")


def test_ayudantes_del_strip_no_reventan_con_un_type_dict():
    # `_has_part_type` recorre `content`, no `tools`: hay que poner la parte
    # defectuosa ahi, si no el test no ejercita su camino.
    body = {"messages": [{"role": "user", "content": [
        {"type": {"json_schema": {}}},
        {"type": "text", "text": "hola"},
    ]}]}
    assert STRIP._has_part_type(body, {"image"}) is False
    assert STRIP._has_part_type(_body_con_propiedad_type(), {"image"}) is False
    assert STRIP._is_structured_output(
        {"response_format": {"type": {"json_schema": {}}}}) is False
    assert STRIP._is_structured_output(
        {"response_format": {"type": "json_schema"}}) is True
