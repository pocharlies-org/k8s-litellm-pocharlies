"""Contrato del `/effort` del cliente sobre el nivel de pensamiento.

Los tres perfiles del residente (`tooling`/`high`/`max`) NO son tres modelos:
son tres niveles del mismo checkpoint, y hasta ahora el unico modo de
subir el nivel era cambiar de modelo en el selector. Este contrato fija la mitad
de servidor del `/effort` de OpenClaw: un `reasoning_effort` traducible gana al
nombre del alias.

Lo que se fija aqui, y por que cada cosa:

* 05-09-2026 (SC-203): la regla de "solo los deliberados" se RETIRA para
  `low`/`medium`. Su premisa era que `tooling` estaba etiquetado "sin pensar" y
  un `low` ambiente lo pondria a pensar en cada turno; hoy el default del
  residente es `low` (THINKING_TIERS), asi que ambiente y pedido son el mismo
  nivel y traducir no cambia nada. `none`/`off` siguen pudiendo apagar, y un
  effort deliberado gana al alias igual que antes.
* `medium` entra con el menu oficial de Qwen3.8 (xhigh/medium/low): anunciarlo
  y no traducirlo es el fallo de las 56 fugas del 19-08. Sobre el 27B del
  perfil creative (misma familia qwen a efectos del hook) el motor lo resuelve
  como "no piensa" -- medido 15-08: reasoning_content 0 --, igual que hacia
  antes al caer fuera de la tabla.
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


def ctk(hook, body, alias, backend="openai/qwen38-flash-next"):
    """Los chat_template_kwargs que salen para el residente."""
    _install_fake_litellm(backend)
    data = dict(body)
    hook._apply_thinking_tier(data, alias)
    return (data.get("extra_body") or {}).get("chat_template_kwargs") or {}


# 05-09-2026 (SC-203): sonda para los casos de "effort desconocido". Ya no
# puede ser `tooling` — su default es `low`, asi que un desconocido (cae al
# default) y un `low` deliberado dan el MISMO resultado y el test no
# distinguiria "manda el alias" de "manda low". `max` tampoco vale: el alias
# resuelto tiene que ser qwen-shaped para que la traduccion exista, y `max` ya
# no es un alias servible. Se construye un alias con tier propio y backend
# qwen: mismo papel que jugaba `tooling` cuando su tier era "off".
DESCONOCIDO_ALIAS = "alias-de-sonda-con-tier-max"


def ctk_sonda(hook, body):
    return ctk(hook, {**body, "model": DESCONOCIDO_ALIAS}, DESCONOCIDO_ALIAS)


@pytest.mark.parametrize("alias,effort,esperado", [
    # El alias dice off y el cliente pide el maximo: manda el cliente. Es el
    # caso que justifica todo esto -- subir el nivel sin cambiar de modelo.
    # 01-09-2026: dialecto de `qwen`, la unica familia que queda. Los valores se
    # derivan de THINKING_KWARGS en el propio test (ver `_kw`) para no fijar a
    # mano un dialecto que aun puede ganar la traduccion de reasoning_effort.
    ("tooling", "max", "max"),
    ("tooling", "high", "high"),
    # 05-09-2026 (SC-203): `low` y `medium` son ya niveles DELIBERADOS del menu
    # oficial, no ambiente. Un `medium` sobre un alias cuyo default es `low`
    # sube de nivel: eso es justo lo que el cliente pide por /effort.
    ("tooling", "low", "low"),
    ("tooling", "medium", "medium"),
    # Bajar tambien: `none` apaga aunque el alias sea el mas caro.
    ("max", "none", "off"),
])
def test_un_effort_deliberado_gana_al_nombre_del_alias(hook, alias, effort, esperado):
    quiere = hook.THINKING_KWARGS["qwen"][esperado]
    assert ctk(hook, {"model": alias, "reasoning_effort": effort}, alias) == quiere


@pytest.mark.parametrize("alias,esperado", [
    # LA REGRESION QUE ESTE TEST EXISTE PARA CAZAR. OpenClaw manda un effort en
    # TODAS las llamadas y a falta de eleccion resuelve a "high" (y los agentes
    # traen thinkingDefault=low). Si un effort NO TRADUCIBLE contara como orden,
    # el alias decidiria. 05-09-2026 (SC-203): `low` ya NO es ambiente — es
    # nivel deliberado del menu oficial y su default, asi que se va al test de
    # arriba. Lo que sigue cayendo fuera de la tabla es lo desconocido, y la
    # sonda es un alias con tier propio (ver `ctk_sonda`): con `tooling` un
    # desconocido y su default `low` dan el mismo resultado y no se distingue
    # "manda el alias" de "manda low".
    (DESCONOCIDO_ALIAS, "max"),
])
@pytest.mark.parametrize("ambiente", ["minimal", "ultra"])
def test_un_effort_ambiente_NO_pisa_al_alias(hook, alias, esperado, ambiente):
    # El alias de la sonda se inyecta en THINKING_TIERS dentro del test: la
    # tabla del hook es la del manifest y alli solo viven alias REALES; una
    # sonda con tier propio no puede entrar en produccion.
    hook.THINKING_TIERS[alias] = esperado
    try:
        quiere = hook.THINKING_KWARGS["qwen"][esperado]
        assert ctk_sonda(hook, {"reasoning_effort": ambiente}) == quiere
    finally:
        hook.THINKING_TIERS.pop(alias, None)


def test_tambien_lee_la_forma_de_la_responses_api(hook):
    """OpenClaw manda `reasoning_effort` plano, pero la responses API lo anida."""
    data = {"model": "tooling", "reasoning": {"effort": "max"}}
    assert ctk(hook, data, "tooling") == hook.THINKING_KWARGS["qwen"]["max"]


@pytest.mark.parametrize("effort,alias,esperado", [
    # 05-09-2026 (SC-203): `medium` ya SE traduce — es nivel oficial del menu y
    # enciende el pensamiento sobre un alias cuyo default es `low`. Lo que no se
    # adivina es lo que NO esta en la tabla: un nombre que el backend no tiene.
    # Sonda con tier propio por el mismo motivo que el test de arriba.
    ("ultra", DESCONOCIDO_ALIAS, "max"),
])
def test_un_effort_sin_traduccion_no_se_adivina(hook, effort, alias, esperado):
    hook.THINKING_TIERS[alias] = esperado
    try:
        quiere = hook.THINKING_KWARGS["qwen"][esperado]
        assert ctk_sonda(hook, {"reasoning_effort": effort}) == quiere
    finally:
        hook.THINKING_TIERS.pop(alias, None)


def test_la_salida_estructurada_gana_a_un_effort_explicito(hook):
    """Pedir `max` y un json_schema a la vez no es mas pensamiento: es una
    contradiccion, y el esquema es el que tiene razon."""
    data = {"model": "max", "reasoning_effort": "max", "response_format": SCHEMA}
    assert ctk(hook, data, "max") == hook.THINKING_KWARGS["qwen"]["off"]


def test_quien_ya_opino_en_chat_template_kwargs_sigue_mandando(hook):
    """La regla nº1 de _apply_thinking_tier no la toca el effort: es por clave."""
    data = {"model": "max", "reasoning_effort": "max",
            "extra_body": {"chat_template_kwargs": {"thinking": False}}}
    assert ctk(hook, data, "max")["thinking"] is False


def test_qwen_no_gradua_y_el_effort_solo_lo_enciende(hook):
    """Qwen enciende, apaga y ACOTA, pero no inventa niveles que no existen.

    Actualizado 31-08-2026: qwen38-flash-next SI gradua. Su chat template hace `reasoning_effort|default('xhigh')` y valida ('xhigh','medium','low'), asi que no traducir dejaba TODO en el maximo. `medium` cae en una rama elif de la plantilla sin `reasoning_instructions` propio (hecho de la plantilla, sigue en pie). CORREGIDO 05-09-2026 (SC-203): el corolario "medium 2721 vs low 2993, indistinguibles" esta refutado — medido hoy via proxy con el razonamiento encendido, medium da reasoning real por encima de low; `high` mapea a `low` porque el backend no tiene nivel `high` (400), no porque medium sea silencio."""
    data = {"model": "tooling", "reasoning_effort": "max"}
    assert ctk(hook, data, "tooling", backend="openai/qwen38-27b") == {
        "enable_thinking": True,
        "reasoning_effort": "xhigh",
    }
    data = {"model": "tooling", "reasoning_effort": "none"}
    assert ctk(hook, data, "tooling", backend="openai/qwen38-27b") == {
        "enable_thinking": False
    }


def test_xhigh_es_sinonimo_de_max_para_el_picker_del_cliente(hook):
    """`xhigh` NO es un nivel nuevo: es el mismo `max` con otro nombre.

    Existe por una limitacion del cliente, no del modelo. El picker de algunos
    clientes de escritorio solo sabe pintar low/medium/high/xhigh; los alias locales
    declaran [none, high, max], asi que `max` se caia del menu y quedaba UNA sola
    opcion util. Con esta entrada el sync puede reetiquetar `max` -> `xhigh` en el
    catalogo que ve ese cliente y el menu vuelve a tener los tres.

    Lo que este test protege es que siga siendo un SINONIMO: si `xhigh` acabara
    traduciendo a otra cosa -- o cayera fuera de la tabla -- seria un effort ambiente,
    decidiria el alias, y para estos modelos el alias es "off". La etiqueta "Muy alto"
    daria MENOS pensamiento que "Alto", que es exactamente el fallo que el comentario
    de los fallbacks de effort describia como motivo para no abrir el hueco.
    """
    assert hook.CLIENT_EFFORT_TIERS["xhigh"] == "max"
    assert hook.CLIENT_EFFORT_TIERS["xhigh"] == hook.CLIENT_EFFORT_TIERS["max"]


def test_el_menu_publicado_y_la_tabla_del_hook_no_se_separan(hook):
    """El menu publicado y la tabla del hook son UNA sola verdad, y desde
    05-09-2026 (SC-203) esa verdad es la receta oficial de Qwen3.8.

    La asercion vieja excluia `low`/`medium` de la tabla. Su decision queda
    documentada en el PR del SC-203 y en el comentario de CLIENT_EFFORT_TIERS:
    se RETIRA porque su premisa ("tooling = sin pensar", un `low` ambiente
    pensaria en cada turno) ya no existe — el default efectivo del residente
    es `low` (THINKING_TIERS), asi que ambiente y pedido son el mismo nivel.
    `medium` entra porque el menu oficial lo anuncia y un nivel anunciado y no
    traducido es el fallo de las 56 fugas del 19-08.

    Lo que el contrato SIGUE fijando, que es lo que siempre fue lo importante:
    cada nivel anunciado tiene traduccion en la tabla, y cada nivel de la tabla
    es un nivel del menu. Si alguien anuncia uno sin traducir —o traduce uno
    que nadie ofrece— esto cae antes de que el nombre mienta en produccion."""
    assert set(hook.CLIENT_EFFORT_TIERS) >= {"none", "off", "low", "medium",
                                             "high", "max", "xhigh"}
    assert set(hook.THINKING_KWARGS["qwen"]) == {"off", "low", "medium",
                                                 "high", "max"}
    for nivel in ("low", "medium", "high", "max"):
        assert hook.CLIENT_EFFORT_TIERS[nivel] == nivel
    # `high`/`max` son alias DEPRECADOS del vocabulario del cliente: siguen
    # traduciendo (high->low, max->xhigh) y por eso siguen anunciados.
    assert hook.CLIENT_EFFORT_TIERS["high"] == "high"
    assert hook.CLIENT_EFFORT_TIERS["max"] == "max"
    # Y el default efectivo de los dos alias del objetivo es `low`.
    assert hook.THINKING_TIERS["tooling"] == "low"
    assert hook.THINKING_TIERS["qwen38-flash-next"] == "low"
