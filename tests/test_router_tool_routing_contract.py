"""Contrato de la regla de enrutado (2026-07-27, decision del owner):

    peticion CON tool incrustada  ->  dense   (uncensored 27B denso)
    peticion SIN tool             ->  tooling (Ornith 35B-A3B MoE)

Se carga el hook REAL desde el manifest y se ejecutan solo sus funciones puras,
sin importar litellm, para que CI valide la logica y no solo el texto.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_has_tools", "_classify_route", "_approx_input_tokens", "_message_entries",
           "_entry_text", "_last_user_entry", "_has_think_marker", "_wants_reasoning",
           "_reasoning_effort_value", "_has_part_type"}
WANT_CONST = {"ROUTE", "AUTO_ROUTED_MODELS", "DENSE_CTX_ESCAPE", "THINK_MARKERS",
              "REASONING_EFFORT_SIGNAL", "TEXT_PART_TYPES", "IMAGE_PART_TYPES",
              "VIDEO_PART_TYPES", "TOOL_ITEM_TYPES", "FAST_CTX_LIMIT"}


@pytest.fixture(scope="module")
def hook():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(d["data"]["litellm_strip_params.py"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("hookpure")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


def _route(hook, data):
    key, rule, _think = hook._classify_route(data)
    return hook.ROUTE[key]["model"], rule


TOOL_DEF = [{"type": "function", "function": {"name": "f"}}]

CASES = [
    ("tools declaradas", {"messages": [{"role": "user", "content": "hola"}], "tools": TOOL_DEF},
     "dense", "tools"),
    ("sin tools", {"messages": [{"role": "user", "content": "hola"}]},
     "tooling", "no-tools"),
    ("tool_choice required", {"messages": [{"role": "user", "content": "x"}],
                             "tool_choice": "required"}, "dense", "tools"),
    ("tool_choice none no cuenta", {"messages": [{"role": "user", "content": "x"}],
                                   "tool_choice": "none"}, "tooling", "no-tools"),
    ("resultado de tool en el historial",
     {"messages": [{"role": "user", "content": "x"}, {"role": "tool", "content": "r"}]},
     "dense", "tools"),
    ("assistant con tool_calls",
     {"messages": [{"role": "assistant", "tool_calls": [{"id": "1"}]}]}, "dense", "tools"),
    ("Responses function_call", {"input": [{"type": "function_call", "name": "f"}]},
     "dense", "tools"),
    ("Responses mcp_call", {"input": [{"type": "mcp_call", "name": "m"}]}, "dense", "tools"),
    ("escape de contexto: el dense corta en 229376",
     {"messages": [{"role": "user", "content": "x" * 900_000}], "tools": TOOL_DEF},
     "tooling", "ctx-escape"),
    ("tools + marcador [think]",
     {"messages": [{"role": "user", "content": "[think] arregla esto"}], "tools": TOOL_DEF},
     "dense-reasoning", "tools+reasoning"),
    ("tools + effort high",
     {"messages": [{"role": "user", "content": "x"}], "tools": TOOL_DEF,
      "reasoning_effort": "high"}, "dense-reasoning", "tools+reasoning"),
    ("tools + effort low es ambiental, no fuerza reasoning",
     {"messages": [{"role": "user", "content": "x"}], "tools": TOOL_DEF,
      "reasoning_effort": "low"}, "dense", "tools"),
    ("sin tools + [think] sigue en Ornith",
     {"messages": [{"role": "user", "content": "[think] piensa"}]}, "tooling", "no-tools"),
    ("multimodal sin tools: Ornith ya admite image:64",
     {"messages": [{"role": "user", "content": [{"type": "image_url",
                                                 "image_url": {"url": "x"}}] * 6}]},
     "tooling", "no-tools"),
]


@pytest.mark.parametrize("name,data,expected_model,expected_rule", CASES,
                         ids=[c[0] for c in CASES])
def test_routing_rule(hook, name, data, expected_model, expected_rule):
    model, rule = _route(hook, data)
    assert (model, rule) == (expected_model, expected_rule)


def test_only_the_auto_aliases_are_rewritten(hook):
    """Una llamada explicita a `dense`/`tooling`/`taxonomy` no se reclasifica."""
    assert hook.AUTO_ROUTED_MODELS == {"router", "auto", "litellmrouter"}
    for explicit in ("dense", "dense-reasoning", "dense-uncensored", "taxonomy",
                     "tooling", "qwen36-35b", "qwen36-35b-tooling"):
        assert explicit not in hook.AUTO_ROUTED_MODELS


def test_fallbacks_cross_the_two_models(hook):
    """dense y tooling se cubren mutuamente: DGX2 es desalojable."""
    assert hook.ROUTE["TOOLS"]["fallback"] == "tooling"
    assert hook.ROUTE["NO_TOOLS"]["fallback"] == "dense"


def test_ctx_escape_stays_under_the_dense_limit(hook):
    """229376 es el max-model-len del uncensored; el margen cubre que
    _approx_input_tokens no cuenta tokens de imagen."""
    assert hook.DENSE_CTX_ESCAPE < 229376
