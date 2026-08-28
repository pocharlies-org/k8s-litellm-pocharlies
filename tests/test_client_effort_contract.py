"""Contrato del `/effort` del cliente sobre el nivel de pensamiento.

Los tres perfiles del residente (`tooling`/`high`/`max`) NO son tres modelos:
son tres niveles del mismo checkpoint, y hasta ahora el unico modo de
subir el nivel era cambiar de modelo en el selector. Este contrato fija la mitad
de servidor del `/effort` de OpenClaw: un `reasoning_effort` traducible gana al
nombre del alias.

Lo que se fija aqui, y por que cada cosa:

* Solo cuentan los efforts DELIBERADOS (high/max, y none para apagar). Los
  clientes agente adjuntan un effort a todas las llamadas, asi que un `low` es
  ambiente, no una orden: si mandara, `tooling` -- "sin pensar" -- pensaria en
  cada turno. Misma regla que REASONING_EFFORT_SIGNAL.
* `medium` no se traduce nunca. DeepSeek no tiene ese nivel y el tokenizer
  parcheado hace caer lo desconocido a "low", asi que seria un nombre que
  miente. Por eso tampoco esta en el menu del cliente.
* La salida estructurada gana incluso a un effort explicito. Medido 3/3 el
  2026-08-10: con thinking activo se cuela una llave del razonamiento delante
  del JSON guiado y el parse revienta.
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


def ctk(hook, body, alias, backend="openai/deepseek-v4-flash-0731"):
    """Los chat_template_kwargs que salen para el residente."""
    _install_fake_litellm(backend)
    data = dict(body)
    hook._apply_thinking_tier(data, alias)
    return (data.get("extra_body") or {}).get("chat_template_kwargs") or {}


@pytest.mark.parametrize("alias,effort,esperado", [
    # El alias dice off y el cliente pide el maximo: manda el cliente. Es el
    # caso que justifica todo esto -- subir el nivel sin cambiar de modelo.
    ("tooling", "max", {"thinking": True, "reasoning_effort": "max"}),
    ("tooling", "high", {"thinking": True, "reasoning_effort": "high"}),
    # Bajar tambien: `none` apaga aunque el alias sea el mas caro.
    ("max", "none", {"thinking": False}),
])
def test_un_effort_deliberado_gana_al_nombre_del_alias(hook, alias, effort, esperado):
    assert ctk(hook, {"model": alias, "reasoning_effort": effort}, alias) == esperado


@pytest.mark.parametrize("alias,esperado", [
    # LA REGRESION QUE ESTE TEST EXISTE PARA CAZAR. OpenClaw manda un effort en
    # TODAS las llamadas y a falta de eleccion resuelve a "high" (y los agentes
    # traen thinkingDefault=low). Si un effort ambiente contara como orden,
    # `tooling` -- "sin pensar", primary de culturismo e image-cloud -- pensaria
    # en cada turno y la etiqueta del selector mentiria.
    ("tooling", {"thinking": False}),
])
@pytest.mark.parametrize("ambiente", ["low", "minimal"])
def test_un_effort_ambiente_NO_pisa_al_alias(hook, alias, esperado, ambiente):
    assert ctk(hook, {"model": alias, "reasoning_effort": ambiente}, alias) == esperado


def test_tambien_lee_la_forma_de_la_responses_api(hook):
    """OpenClaw manda `reasoning_effort` plano, pero la responses API lo anida."""
    data = {"model": "tooling", "reasoning": {"effort": "max"}}
    assert ctk(hook, data, "tooling") == {"thinking": True, "reasoning_effort": "max"}


@pytest.mark.parametrize("effort,alias,esperado", [
    # `medium` no se traduce por su cuenta: con un alias que no piensa, no lo
    # enciende. Es el nivel que no existe en DeepSeek.
    ("medium", "tooling", {"thinking": False}),
])
def test_un_effort_sin_traduccion_no_se_adivina(hook, effort, alias, esperado):
    assert ctk(hook, {"model": alias, "reasoning_effort": effort}, alias) == esperado


def test_la_salida_estructurada_gana_a_un_effort_explicito(hook):
    """Pedir `max` y un json_schema a la vez no es mas pensamiento: es una
    contradiccion, y el esquema es el que tiene razon."""
    data = {"model": "max", "reasoning_effort": "max", "response_format": SCHEMA}
    assert ctk(hook, data, "max") == {"thinking": False}


def test_quien_ya_opino_en_chat_template_kwargs_sigue_mandando(hook):
    """La regla nº1 de _apply_thinking_tier no la toca el effort: es por clave."""
    data = {"model": "max", "reasoning_effort": "max",
            "extra_body": {"chat_template_kwargs": {"thinking": False}}}
    assert ctk(hook, data, "max")["thinking"] is False


def test_qwen_no_gradua_y_el_effort_solo_lo_enciende(hook):
    """Qwen solo tiene `enable_thinking`. low/high/max colapsan a "piensa" y
    `none` sigue apagando: el effort no puede inventar niveles que no existen."""
    data = {"model": "tooling", "reasoning_effort": "max"}
    assert ctk(hook, data, "tooling", backend="openai/qwen38-27b") == {
        "enable_thinking": True
    }
    data = {"model": "tooling", "reasoning_effort": "none"}
    assert ctk(hook, data, "tooling", backend="openai/qwen38-27b") == {
        "enable_thinking": False
    }


def test_xhigh_es_sinonimo_de_max_para_el_picker_de_codex(hook):
    """`xhigh` NO es un nivel nuevo: es el mismo `max` con otro nombre.

    Existe por una limitacion del cliente, no del modelo. El picker de Codex Desktop
    solo sabe pintar low/medium/high/xhigh; los alias locales declaran
    [none, high, max], asi que `max` se caia del menu y quedaba UNA sola opcion util.
    Con esta entrada el sync puede reetiquetar `max` -> `xhigh` en el catalogo de
    Codex y el menu vuelve a tener los tres.

    Lo que este test protege es que siga siendo un SINONIMO: si `xhigh` acabara
    traduciendo a otra cosa -- o cayera fuera de la tabla -- seria un effort ambiente,
    decidiria el alias, y para estos modelos el alias es "off". La etiqueta "Muy alto"
    daria MENOS pensamiento que "Alto", que es exactamente el fallo que el comentario
    de CODEX_EFFORT_FALLBACKS describia como motivo para no abrir el hueco.
    """
    assert hook.CLIENT_EFFORT_TIERS["xhigh"] == "max"
    assert hook.CLIENT_EFFORT_TIERS["xhigh"] == hook.CLIENT_EFFORT_TIERS["max"]


def test_el_menu_publicado_y_la_tabla_del_hook_no_se_separan(hook):
    """`medium` fuera de la tabla es la mitad del contrato; la otra mitad es que
    los tres niveles que SI se ofrecen esten aqui. Si alguien mete `medium`, este
    test cae antes de que el nombre empiece a mentir en produccion."""
    assert set(hook.CLIENT_EFFORT_TIERS) == {"none", "off", "high", "max", "xhigh"}
    assert set(hook.THINKING_KWARGS["deepseek-v4"]) == {"off", "low", "high", "max"}
    for nivel in ("high", "max"):
        assert hook.CLIENT_EFFORT_TIERS[nivel] == nivel
    # `low` fuera es la mitad del contrato: es el valor ambiente y no puede
    # convertirse en una orden. `medium` fuera es la otra mitad.
    assert "low" not in hook.CLIENT_EFFORT_TIERS
    assert "medium" not in hook.CLIENT_EFFORT_TIERS
