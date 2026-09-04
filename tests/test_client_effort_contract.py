"""Contrato del `/effort` del cliente sobre el nivel de pensamiento.

03-09-2026: decision del owner — «la receta oficial de Qwen3.8 es
[low, medium, xhigh]; quiero esa en LiteLLM y que los clientes usen lo de
LiteLLM». El menu publicado (`supported_reasoning_efforts`) y lo que el hook
deja pasar son UNA sola verdad: los niveles de la receta viajan PASA-CRUDO al
motor, y `none` es `enable_thinking: false` (el unico que el hook sigue
escribiendo, porque no es un effort del modelo).

Lo que se fija aqui, y por que cada cosa:

* La receta [low, medium, xhigh] + `none` esta en CLIENT_EFFORT_TIERS y en
  THINKING_KWARGS["qwen"] con el MISMO valor. Si alguien vuelve a traducir
  (`xhigh -> max`), este test cae: esa traduccion es justo la que el owner
  quito.
* El effort pasa-crudo NO se borra del cuerpo (strip): el valor que anuncia
  /model/info es el que ve el backend.
* Sigue habiendo effort AMBIENTE: un valor fuera de la tabla (p.ej. `minimal`)
  no es una orden, decide el alias. `tooling` sigue sin pensar por defecto.
* La salida estructurada gana incluso a un effort explicito. Medido 3/3 el
  2026-08-10: con thinking activo se cuela una llave del razonamiento delante
  del JSON guiado y el parse revienta.
* Con tools sobre la familia qwen gana enable_thinking:false (sglang#36537):
  eso no traduce el nivel, lo apaga, y el menu no lo puede prometer.
* Quien ya opino en `chat_template_kwargs` sigue mandando, effort o no.

Se carga el hook REAL del manifest y se ejecutan solo sus funciones puras, igual
que el resto de los contratos de este repo.
"""
import ast
import logging
import sys
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_apply_thinking_tier", "_client_thinking_tier",
           "_reasoning_effort_value", "_family_of_alias",
           # 2026-08-28: `_apply_thinking_tier` apaga el pensamiento cuando la
           # peticion lleva tools (sglang#36537). Sin exportar `_has_tools` su
           # NameError cae en el except de la funcion y el tier deja de
           # aplicarse ENTERO, no solo esa rama.
           "_is_structured_output", "_has_tools"}
WANT_CONST = {"THINKING_TIERS", "THINKING_KWARGS", "CLIENT_EFFORT_TIERS",
              # `_family_of_alias` resuelve la familia contra esta tabla.
              "FAMILY_SAMPLING"}

SCHEMA = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}

# La receta oficial de Qwen3.8 (model cards HF del denso y del flash-next,
# identicas) mas `none` = enable_thinking:false.
RECETA = ("low", "medium", "xhigh")


def _install_fake_litellm(model):
    """`_family_of_alias` importa el router DENTRO de la funcion."""

    class FakeRouter:
        def get_model_list(self, model_name=None):
            return [{"litellm_params": {"model": model}}]

    proxy_server = types.ModuleType("litellm.proxy.proxy_server")
    proxy_server.llm_router = FakeRouter()
    proxy = types.ModuleType("litellm.proxy")
    proxy.proxy_server = proxy_server
    litellm = types.ModuleType("litellm")
    litellm.proxy = proxy
    sys.modules["litellm"] = litellm
    sys.modules["litellm.proxy"] = proxy
    sys.modules["litellm.proxy.proxy_server"] = proxy_server


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
    found = {t.id for n in keep if isinstance(n, ast.Assign)
             for t in n.targets if getattr(t, "id", None)}
    assert WANT_CONST <= found, f"el hook ya no define: {sorted(WANT_CONST - found)}"
    mod = types.ModuleType("effortpure")
    mod.log = logging.getLogger("test.hook")
    mod.sampling_log = logging.getLogger("test.hook.sampling")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


def ctk(hook, body, alias, backend="openai/qwen38-flash-next"):
    """Los chat_template_kwargs que salen para el residente."""
    _install_fake_litellm(backend)
    data = dict(body)
    hook._apply_thinking_tier(data, alias)
    return (data.get("extra_body") or {}).get("chat_template_kwargs") or {}


@pytest.mark.parametrize("nivel", RECETA)
def test_la_receta_oficial_viaja_pasa_crudo(hook, nivel):
    """El menu y el backend son la misma verdad: low/medium/xhigh no se traducen.

    El hook escribe el effort en chat_template_kwargs con el MISMO nombre y NO
    borra el `reasoning_effort` del cliente: lo que anuncia /model/info es
    literalmente lo que recibe el motor.
    """
    assert hook.CLIENT_EFFORT_TIERS[nivel] == nivel
    assert hook.THINKING_KWARGS["qwen"][nivel] == {
        "enable_thinking": True, "reasoning_effort": nivel}
    _install_fake_litellm("openai/qwen38-flash-next")
    data = {"model": "tooling", "reasoning_effort": nivel}
    hook._apply_thinking_tier(data, "tooling")
    assert data.get("reasoning_effort") == nivel, "el strip volvio: el menu mentiria"
    assert ctk(hook, {"model": "tooling", "reasoning_effort": nivel}, "tooling") == {
        "enable_thinking": True, "reasoning_effort": nivel
    }


def test_none_es_enable_thinking_false(hook):
    """`none` no es un effort del modelo: es el interruptor del chat template."""
    assert hook.CLIENT_EFFORT_TIERS["none"] == "off"
    assert hook.THINKING_KWARGS["qwen"]["off"] == {"enable_thinking": False}
    data = {"model": "tooling", "reasoning_effort": "none"}
    hook._apply_thinking_tier(data, "tooling")
    assert data.get("extra_body", {}).get("chat_template_kwargs") == {"enable_thinking": False}
    # Apagado: `none` no es un nivel del motor (es enable_thinking:false), asi
    # que el effort no describe lo enviado y el strip lo quita: no debe quedar
    # flotando en una peticion que va sin pensamiento.
    assert "reasoning_effort" not in data


@pytest.mark.parametrize("alias,effort,esperado", [
    # El alias dice off y el cliente pide xhigh: manda el cliente, sin cambiar
    # de modelo. Es el caso que justifica todo esto.
    ("tooling", "xhigh", "xhigh"),
    # Bajar tambien: `none` apaga aunque el alias sea el mas caro.
    ("max", "none", "off"),
])
def test_un_effort_deliberado_gana_al_nombre_del_alias(hook, alias, effort, esperado):
    data = {"model": alias, "reasoning_effort": effort}
    result = ctk(hook, data, alias)
    if esperado == "off":
        assert result == hook.THINKING_KWARGS["qwen"]["off"]
    else:
        assert result == hook.THINKING_KWARGS["qwen"][esperado]


@pytest.mark.parametrize("alias,esperado", [
    # LA REGRESION QUE ESTE TEST EXISTE PARA CAZAR. OpenClaw manda un effort en
    # TODAS las llamadas y a falta de eleccion resuelve a un valor que ya no
    # esta en la receta (los agentes traian thinkingDefault=low). Si un effort
    # ambiente contara como orden, `tooling` -- "sin pensar" -- pensaria en cada
    # turno y la etiqueta del selector mentiria.
    ("tooling", "off"),
])
@pytest.mark.parametrize("ambiente", ["minimal", "ultra", "medium-bien?"])
def test_un_effort_ambiente_NO_pisa_al_alias(hook, alias, esperado, ambiente):
    """Ambiente = lo que no esta en CLIENT_EFFORT_TIERS. `low` y `medium` ya NO
    son ambiente: desde el 03-09 son orden deliberada (ver test de la receta)."""
    assert ambiente not in hook.CLIENT_EFFORT_TIERS
    assert ctk(hook, {"model": alias, "reasoning_effort": ambiente}, alias) == \
        hook.THINKING_KWARGS["qwen"][esperado]


def test_tambien_lee_la_forma_de_la_responses_api(hook):
    """OpenClaw manda `reasoning_effort` plano, pero la responses API lo anida."""
    data = {"model": "tooling", "reasoning": {"effort": "xhigh"}}
    assert ctk(hook, data, "tooling") == hook.THINKING_KWARGS["qwen"]["xhigh"]
    assert data["reasoning"].get("effort") == "xhigh", "forma anidada: pasa-crudo"


def test_high_y_max_solo_viven_para_deepseek(hook):
    """`high`/`max` siguen en la tabla por DeepSeek (su vocabulario es
    low/high/max), pero la familia qwen NO los traduce: el motor contesta 400
    y ese 400 honesto es preferible a una traduccion nuestra."""
    assert hook.CLIENT_EFFORT_TIERS["high"] == "high"
    assert hook.CLIENT_EFFORT_TIERS["max"] == "max"
    assert "high" not in hook.THINKING_KWARGS["qwen"]
    assert "max" not in hook.THINKING_KWARGS["qwen"]
    # Sobre un backend qwen: sin traduccion, sin escribir nada, sin strip.
    data = {"model": "tooling", "reasoning_effort": "high"}
    assert ctk(hook, data, "tooling") == {}
    assert data.get("reasoning_effort") == "high"


def test_la_salida_estructurada_gana_a_un_effort_explicito(hook):
    """Pedir `xhigh` y un json_schema a la vez no es mas pensamiento: es una
    contradiccion, y el esquema es el que tiene razon."""
    data = {"model": "tooling", "reasoning_effort": "xhigh",
            "response_format": SCHEMA}
    assert ctk(hook, data, "tooling") == hook.THINKING_KWARGS["qwen"]["off"]


def test_tools_sobre_qwen_apagan_el_pensamiento(hook):
    """sglang#36537: thinking + tools + qwen3_coder = bucle de token 0. No es
    traduccion del nivel: es apagarlo, y el menu no puede prometerlo."""
    data = {"model": "tooling", "reasoning_effort": "xhigh",
            "tools": [{"type": "function", "function": {"name": "f"}}]}
    assert ctk(hook, data, "tooling") == hook.THINKING_KWARGS["qwen"]["off"]


def test_quien_ya_opino_en_chat_template_kwargs_sigue_mandando(hook):
    """La regla nº1 de _apply_thinking_tier no la toca el effort: es por clave."""
    data = {"model": "tooling", "reasoning_effort": "xhigh",
            "extra_body": {"chat_template_kwargs": {"thinking": False}}}
    assert ctk(hook, data, "tooling")["thinking"] is False


def test_el_menu_publicado_y_la_tabla_del_hook_no_se_separan(hook):
    """El contrato de UNA sola verdad, al reves que antes: la tabla del hook
    cubre la receta + none, y lo que no este aqui no se traduce nunca."""
    assert set(hook.CLIENT_EFFORT_TIERS) >= {"none", "off", *RECETA}
    for nivel in RECETA:
        assert hook.CLIENT_EFFORT_TIERS[nivel] == nivel
    assert set(hook.THINKING_KWARGS["qwen"]) == {"off", *RECETA}
