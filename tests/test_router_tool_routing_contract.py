"""Contrato del selector automatico de profundidad (2026-08-13, owner):

    router -> tooling / agent / high / max segun la complejidad

Las tools DECLARADAS no cuentan: OpenClaw adjunta su catalogo entero incluso a
una pregunta trivial. Solo cuentan la intencion y las ejecuciones reales.

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
           "_entry_text", "_last_user_entry", "_has_think_marker",
           "_reasoning_effort_value", "_has_part_type", "_is_structured_output",
           "_contains_router_hint", "_has_executed_tool_history",
           # _degrade walks the fallback chain; _alias_has_deployments is its default
           # probe and has to exist for _degrade's signature to bind at def time. Its
           # body lazily imports litellm, so pulling it in costs nothing here -- the
           # chain tests inject a stub instead of calling it.
           "_degrade", "_alias_has_deployments", "_walk_chain", "_chain_of"}
WANT_CONST = {"ROUTE", "AUTO_ROUTED_MODELS", "THINK_MARKERS",
              "REASONING_EFFORT_SIGNAL", "TEXT_PART_TYPES", "IMAGE_PART_TYPES",
              "VIDEO_PART_TYPES", "TOOL_ITEM_TYPES", "CAPABILITY_CHAINS",
              "THINKING_TIERS", "ROUTER_MAX_HINTS", "ROUTER_HIGH_HINTS",
              "ROUTER_LOW_HINTS", "ROUTER_OFF_HINTS",
              "ROUTER_HIGH_CONTEXT_TOKENS", "ROUTER_MAX_CONTEXT_TOKENS"}


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
    ("tools declaradas no cambian pregunta trivial",
     {"messages": [{"role": "user", "content": "hola"}], "tools": TOOL_DEF},
     "tooling", "short-simple"),
    ("sin tools trivial", {"messages": [{"role": "user", "content": "hola"}]},
     "tooling", "short-simple"),
    ("tool_choice required", {"messages": [{"role": "user", "content": "x"}],
                             "tool_choice": "required"}, "tooling", "short-simple"),
    ("tool_choice none no cuenta", {"messages": [{"role": "user", "content": "x"}],
                                   "tool_choice": "none"}, "tooling", "short-simple"),
    ("resultado de tool en el historial",
     {"messages": [{"role": "user", "content": "x"}, {"role": "tool", "content": "r"}]},
     "high", "execution-history"),
    ("assistant con tool_calls",
     {"messages": [{"role": "user", "content": "continua"},
                   {"role": "assistant", "tool_calls": [{"id": "1"}]}]},
     "high", "execution-history"),
    ("tool antigua no fija el turno nuevo en high",
     {"messages": [{"role": "user", "content": "haz algo"},
                   {"role": "assistant", "tool_calls": [{"id": "1"}]},
                   {"role": "tool", "content": "hecho"},
                   {"role": "user", "content": "gracias"}]},
     "tooling", "short-simple"),
    ("Responses function_call", {"input": [{"type": "function_call", "name": "f"}]},
     "high", "execution-history"),
    ("Responses mcp_call", {"input": [{"type": "mcp_call", "name": "m"}]},
     "high", "execution-history"),
    ("Responses tool antigua no fija el turno nuevo en high",
     {"input": [{"role": "user", "content": "haz algo"},
                {"type": "function_call", "name": "f"},
                {"type": "function_call_output", "output": "hecho"},
                {"role": "user", "content": "gracias"}]},
     "tooling", "short-simple"),
    ("marcador [think]",
     {"messages": [{"role": "user", "content": "[think] arregla esto"}], "tools": TOOL_DEF},
     "high", "think-marker"),
    ("effort high",
     {"messages": [{"role": "user", "content": "x"}], "tools": TOOL_DEF,
      "reasoning_effort": "high"}, "high", "explicit-high"),
    ("effort xhigh", {"messages": [{"role": "user", "content": "x"}],
                       "reasoning_effort": "xhigh"}, "max", "explicit-max"),
    ("effort low ambiental no fuerza profundidad",
     {"messages": [{"role": "user", "content": "x"}], "tools": TOOL_DEF,
      "reasoning_effort": "low"}, "tooling", "short-simple"),
    ("explicacion normal",
     {"messages": [{"role": "user", "content": "explica como funciona el router"}]},
     "agent", "reasoned-intent"),
    ("implementacion compleja",
     {"messages": [{"role": "user", "content": "implementa y despliega este cambio"}]},
     "high", "complex-intent"),
    ("maximo explicito",
     {"messages": [{"role": "user", "content": "auditoria exhaustiva con maxima profundidad"}]},
     "max", "explicit-max"),
    ("salida estructurada siempre sin pensar",
     {"messages": [{"role": "user", "content": "audita esto con maxima profundidad"}],
      "response_format": {"type": "json_schema"}}, "tooling", "structured-output"),
    ("peticion no trivial sin señal usa ligero",
     {"messages": [{"role": "user", "content": "redacta una respuesta amable " + "x" * 100}]},
     "agent", "default-low"),
    ("multimodal trivial",
     {"messages": [{"role": "user", "content": [{"type": "image_url",
                                                 "image_url": {"url": "x"}}] * 6}]},
     "tooling", "short-simple"),
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


def test_router_solo_selecciona_los_cuatro_tiers(hook):
    assert {entry["model"] for entry in hook.ROUTE.values()} == set(hook.THINKING_TIERS)
    for category in hook.ROUTE:
        chain = _chain(hook, category)
        assert chain[-1] == "cloudblue/gpt-5.6-luna"
        assert not any(alias.startswith("dense") for alias in chain)


def test_every_chain_ends_in_independent_cloud_fallback(hook):
    for category in hook.ROUTE:
        chain = _chain(hook, category)
        assert len(chain) >= 2, f"{category} no tiene a donde degradar: {chain}"
        assert len(set(chain)) == len(chain), f"{category} repite un alias: {chain}"
        assert chain[-1] == "cloudblue/gpt-5.6-luna", chain


def test_degrade_walks_to_the_first_live_alias(hook):
    """Recorrido con sondas inyectadas: primario, degradado y cadena seca."""
    tools = _chain(hook, "HIGH")

    assert hook._degrade("HIGH", alias_live=lambda a: True) == (tools[0], "primary")

    # Solo el ultimo vivo -> tiene que llegar hasta el, no pararse en el primero.
    assert hook._degrade("HIGH", alias_live=lambda a: a == tools[-1]) \
        == (tools[-1], "degraded")

    # Cadena seca: devuelve el primario y lo marca, en vez de despachar a ciegas.
    assert hook._degrade("HIGH", alias_live=lambda a: False) == (tools[0], "dry")


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
    # 2026-08-10: entran `agent`, `high` y `max`. Siguen siendo nombres de
    # CAPACIDAD -- no dicen QUE modelo, dicen cuanto piensa el residente -- y los
    # registra y borra el mismo sync que a `tooling`, asi que comparten su modo de
    # fallo: sin cadena, un residente caido sale como 400 en vez de degradar.
    assert set(hook.CAPABILITY_CHAINS) == {"tooling", "agent", "high", "max"}, (
        "solo los nombres de capacidad degradan; anadir un nombre de modelo aqui "
        "falsearia las mediciones hechas contra el")
    # Cada uno es cabeza de su propia cadena: el tier se calcula sobre el alias que
    # pidio el cliente, asi que la cabeza NO puede colapsar a `tooling` (eso
    # convertiria un `max` degradado en "sin pensar", que es el tier contrario).
    for cap in ("tooling", "agent", "high", "max"):
        assert hook.CAPABILITY_CHAINS[cap]["model"] == cap
    # Y los cuatro tienen que estar en la tabla de tiers, o el nombre no significa
    # nada: seria un alias mas apuntando al mismo sitio.
    assert set(hook.CAPABILITY_CHAINS) == set(hook.THINKING_TIERS)
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
    assert hook._walk_chain(entry, alias_live=lambda a: a == "cloudblue/gpt-5.6-luna") \
        == ("cloudblue/gpt-5.6-luna", "degraded")
    assert hook._walk_chain(entry, alias_live=lambda a: False) == ("tooling", "dry")


def test_degrade_probes_each_alias_at_most_once(hook):
    """Una pasada, no un retry: sin esto el cruce mutuo si podria ciclar."""
    seen = []

    def probe(alias):
        seen.append(alias)
        return False

    hook._degrade("LOW", alias_live=probe)
    assert seen == list(_chain(hook, "LOW"))
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

    for profile in ("tooling", "agent", "high", "max"):
        assert graph.get(profile) == ["cloudblue/gpt-5.6-luna"], (
            f"se esperaba {profile} -> [cloudblue/gpt-5.6-luna], "
            f"hay {graph.get(profile)!r}")

    # Recorrido completo: ningun camino puede volver a un nodo ya visitado.
    def walk(node, seen):
        assert node not in seen, f"ciclo de fallbacks: {' -> '.join(seen + [node])}"
        for nxt in graph.get(node, []):
            walk(nxt, seen + [node])

    for src in graph:
        walk(src, [])
