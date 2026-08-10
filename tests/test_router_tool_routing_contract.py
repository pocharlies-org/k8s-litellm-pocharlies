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
           "_reasoning_effort_value", "_has_part_type",
           # _degrade walks the fallback chain; _alias_has_deployments is its default
           # probe and has to exist for _degrade's signature to bind at def time. Its
           # body lazily imports litellm, so pulling it in costs nothing here -- the
           # chain tests inject a stub instead of calling it.
           "_degrade", "_alias_has_deployments", "_walk_chain", "_chain_of"}
WANT_CONST = {"ROUTE", "AUTO_ROUTED_MODELS", "DENSE_CTX_ESCAPE", "THINK_MARKERS",
              "REASONING_EFFORT_SIGNAL", "TEXT_PART_TYPES", "IMAGE_PART_TYPES",
              "VIDEO_PART_TYPES", "TOOL_ITEM_TYPES", "FAST_CTX_LIMIT",
              "CAPABILITY_CHAINS"}


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


def _chain(hook, category):
    entry = hook.ROUTE[category]
    return (entry["model"],) + tuple(entry.get("fallbacks") or ())


def test_fallbacks_cross_the_two_models(hook):
    """dense y tooling se siguen cubriendo mutuamente: DGX2 es desalojable.

    Aqui es seguro que el cruce sea mutuo porque _degrade recorre la cadena UNA
    vez antes de despachar; no es un retry y no puede hacer ping-pong.
    """
    assert "tooling" in _chain(hook, "TOOLS")
    assert "dense" in _chain(hook, "NO_TOOLS")


def test_every_chain_ends_outside_its_own_node(hook):
    """La invariante que faltaba el 2026-08-04.

    El bug no era el cruce: era que se degradaba al fallback SIN comprobarlo, y
    con los dos nodos a medio swap se despachaba a un alias desregistrado. Que
    cada cadena tenga >= 2 saltos y toque los dos nodos es lo que impide que
    degradar sea caer en un agujero. dense/dense-reasoning/qwen3-coder viven en
    DGX2; tooling en DGX1.
    """
    dgx2 = {"dense", "dense-reasoning", "qwen3-coder"}
    for category in hook.ROUTE:
        chain = _chain(hook, category)
        assert len(chain) >= 2, f"{category} no tiene a donde degradar: {chain}"
        assert len(set(chain)) == len(chain), f"{category} repite un alias: {chain}"
        assert set(chain) & dgx2 and set(chain) - dgx2, (
            f"{category} no sale de un solo nodo: {chain}")


def test_degrade_walks_to_the_first_live_alias(hook):
    """Recorrido con sondas inyectadas: primario, degradado y cadena seca."""
    tools = _chain(hook, "TOOLS")

    assert hook._degrade("TOOLS", alias_live=lambda a: True) == (tools[0], "primary")

    # Solo el ultimo vivo -> tiene que llegar hasta el, no pararse en el primero.
    assert hook._degrade("TOOLS", alias_live=lambda a: a == tools[-1]) \
        == (tools[-1], "degraded")

    # Cadena seca: devuelve el primario y lo marca, en vez de despachar a ciegas.
    assert hook._degrade("TOOLS", alias_live=lambda a: False) == (tools[0], "dry")


def test_solo_tooling_degrada_y_los_nombres_de_modelo_no(hook):
    """La linea que separa un nombre de CAPACIDAD de un nombre de MODELO.

    Reescrito 2026-08-10, al retirar `tooling-ha` y `agentic-ha`. El argumento viejo
    era que degradar `tooling` corromperia las mediciones, y de ahi un alias -ha
    aparte. Se cae: las mediciones se hacen contra nombres de MODELO (`ornith-1.0`,
    `qwen3-coder`, `qwen36-35b-nvfp4`, `dense-uncensored`), que NO estan en esta
    tabla y por tanto siguen dando 400 cuando su modelo no esta. `tooling` es una
    capacidad, y una capacidad que da 400 en vez de degradar no protege nada.

    Lo que este test impide es que alguien meta un nombre de MODELO aqui: eso si
    volveria a falsear medidas en silencio.
    """
    assert set(hook.CAPABILITY_CHAINS) == {"tooling"}, (
        "solo los nombres de capacidad degradan; anadir un nombre de modelo aqui "
        "falsearia las mediciones hechas contra el")
    assert hook.CAPABILITY_CHAINS["tooling"]["model"] == "tooling"
    # Disjunto de los auto-enrutados: si un nombre cayera en los dos, la rama de
    # capacidad correria primero y la clasificacion por forma de peticion no se
    # aplicaria nunca.
    assert not (set(hook.CAPABILITY_CHAINS) & hook.AUTO_ROUTED_MODELS)
    for explicit in ("ornith-1.0", "ornith-canary", "qwen3-coder",
                     "qwen36-35b-nvfp4", "dense-uncensored", "taxonomy",
                     "tooling-ha", "agentic-ha"):
        assert explicit not in hook.CAPABILITY_CHAINS, (
            f"{explicit} degradaria en silencio")


def test_tooling_es_no_op_cuando_esta_registrado(hook):
    """Lo importante de fusionar el -ha dentro de `tooling`: en el caso normal no
    cambia NADA. Solo actua cuando el alias no esta, que es el hueco donde antes
    salia un 400 duro que `router_settings.fallbacks` no puede cubrir, porque el
    proxy rechaza el nombre antes de que corra el Router."""
    entry = hook.CAPABILITY_CHAINS["tooling"]
    assert hook._walk_chain(entry, alias_live=lambda a: True) == ("tooling", "primary")
    assert hook._walk_chain(entry, alias_live=lambda a: a == "dense") == ("dense", "degraded")
    assert hook._walk_chain(entry, alias_live=lambda a: False) == ("tooling", "dry")


def test_degrade_probes_each_alias_at_most_once(hook):
    """Una pasada, no un retry: sin esto el cruce mutuo si podria ciclar."""
    seen = []

    def probe(alias):
        seen.append(alias)
        return False

    hook._degrade("NO_TOOLS", alias_live=probe)
    assert seen == list(_chain(hook, "NO_TOOLS"))
    assert len(seen) == len(set(seen))


@pytest.fixture(scope="module")
def proxy_config():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    raw = next(d["data"]["config.yaml"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    return yaml.safe_load(raw)


def test_proxy_fallbacks_are_acyclic(proxy_config):
    """El fallback de la config SI es un retry: un ciclo aqui gira para siempre.

    Cubre el modo de fallo que el hook NO puede ver -- alias registrado pero sin
    deployment sano ("no healthy deployments for this model"), que es el que tumbo
    a Aurora el 2026-08-04 19:11. Un solo sentido es un DAG; anadir el inverso es
    el ping-pong contra el que avisaba el comentario viejo, y este test es lo que
    impide que vuelva.
    """
    entries = proxy_config.get("router_settings", {}).get("fallbacks") or []
    graph = {}
    for entry in entries:
        for src, dsts in entry.items():
            graph.setdefault(src, []).extend(dsts or [])

    assert graph.get("tooling") == ["dense"], (
        f"se esperaba tooling -> [dense], hay {graph.get('tooling')!r}")

    # Recorrido completo: ningun camino puede volver a un nodo ya visitado.
    def walk(node, seen):
        assert node not in seen, f"ciclo de fallbacks: {' -> '.join(seen + [node])}"
        for nxt in graph.get(node, []):
            walk(nxt, seen + [node])

    for src in graph:
        walk(src, [])


def test_ctx_escape_stays_under_the_dense_limit(hook):
    """229376 es el max-model-len del uncensored; el margen cubre que
    _approx_input_tokens no cuenta tokens de imagen."""
    assert hook.DENSE_CTX_ESCAPE < 229376
