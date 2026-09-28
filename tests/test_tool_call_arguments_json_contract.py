"""Contrato del saneado de `function.arguments` del historial.

POR QUE EXISTE (27-09-2026)
---------------------------
Un turno moria con 400 al rebotar a Alibaba:

  litellm.BadRequestError: OpenAIException - The "function.arguments" parameter
  of the code model must be in JSON format.
  No fallback model group found for original model_group=alibaba-q38-flash.

Medido contra los DOS backends con el mismo historial (una sonda que manda un
`tool_calls` con `arguments` variados), el resultado es que el rechazo no es de
Alibaba: es de los dos.

  arguments                     residente (vLLM)   Alibaba (Model Studio)
  '{"path":"/etc/hostname"}'    200                 200
  ''                            200                 200
  '{}'                          200                 200
  'no-es-json'                  500                 400
  '"no-es-json"'                500                 400
  '[1,2]'                       500                 400

O sea: el rebote no convierte un fallo de infra en un turno roto — el turno ya
esta roto, y un fallback inverso al residente tampoco lo salva (el veneno viaja
en `messages`). Lo que si lo arregla es normalizar el valor a un objeto JSON
antes de salir, y `{"_raw": <texto>}` esta medido como aceptado.

La propiedad que fija este fichero es la que hace el arreglo INOFENSIVO: una
peticion que hoy funciona tiene que salir byte a byte igual que entro. Si alguien
amplia la reescritura a shapes que hoy funcionan, esto falla en CI y no en
produccion. Y la segunda propiedad, igual de importante: el helper tiene que
estar LLAMADO en el hook (un helper puro que nadie invoca es el modo de fallo
clasico de este repo).

Se carga el hook REAL del manifest y se ejecutan solo sus funciones puras, con un
`log` de mentira, igual que el resto de los contratos de este repo.
"""
import ast
import json
import logging
import pathlib
import types

# CONTRACT: dgx.litellm.tool-call-arguments-json.v1

import pytest
import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_normalize_tool_call_arguments", "_arguments_needs_repair"}

# Lo que HOY funciona en los dos backends: no se toca ni por asomo.
SIN_REESCRIBIR = [
    '{"path": "/etc/hostname"}',  # objeto JSON valido
    "{}",                          # objeto vacio
    "",                            # cadena vacia: la llaman "sin argumentos"
    "   ",                         # y esto tambien
    '{"path": "/etc',              # TRUNCADO: Alibaba lo acepta (medido, 200)
    {"path": "/etc/hostname"},     # dict real: LiteLLM lo serializa el solo
    None,                          # ausente: no es este arreglo el que lo define
]

# Lo que HOY falla contra los dos backends: eso y solo eso se reescribe.
CON_REESCRITURA = [
    "no-es-json",        # prosa
    '"no-es-json"',      # escalar JSON valido, pero no objeto
    "[1,2]",             # array
    "null",              # escalar null
    "123",               # numero
    "true",              # booleano
]


def _hook_source():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def hook():
    src = _hook_source()
    tree = ast.parse(src)
    keep = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in WANT_FN
    ]
    missing = WANT_FN - {n.name for n in keep}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("argumentspure")
    mod.json = json
    mod.log = logging.getLogger("test.hook.arguments")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


def _data_with(arguments):
    return {"model": "alibaba-q38-flash", "messages": [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": arguments}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "gx10-ec3d\n"},
    ]}


@pytest.mark.parametrize("value", SIN_REESCRIBIR)
def test_lo_que_hoy_funciona_sale_intacto(hook, value):
    """La peticion sana no puede cambiar ni un byte: es lo que hace inofensivo el arreglo."""
    data = _data_with(value)
    antes = json.dumps(data, sort_keys=True)
    assert hook._normalize_tool_call_arguments(data) == 0
    assert json.dumps(data, sort_keys=True) == antes
    got = data["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert got == value or (isinstance(value, str) and got.strip() == value.strip())


@pytest.mark.parametrize("value", CON_REESCRITURA)
def test_lo_que_hoy_falla_se_convierte_en_objeto(hook, value):
    """Y el resultado es un objeto JSON, que es lo que los dos backends aceptan."""
    data = _data_with(value)
    assert hook._normalize_tool_call_arguments(data) == 1
    got = data["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(got, str), "tiene que seguir siendo cadena, no dict"
    parsed = json.loads(got)
    assert isinstance(parsed, dict)
    assert parsed["_raw"] == value, "el texto original no se pierde"


def test_es_idempotente(hook):
    """Reescribir dos veces no puede volver a tocar nada (el sticky reenvia el historial)."""
    data = _data_with("no-es-json")
    hook._normalize_tool_call_arguments(data)
    una = json.dumps(data, sort_keys=True)
    assert hook._normalize_tool_call_arguments(data) == 0
    assert json.dumps(data, sort_keys=True) == una


def test_no_reventar_ni_con_shapes_raros(hook):
    """Una peticion no puede morir por este arreglo: formas raras -> 0 y sin excepcion."""
    for data in (
        {},
        {"messages": None},
        {"messages": "no-es-lista"},
        {"messages": [{"role": "assistant"}]},
        {"messages": [{"tool_calls": {"no": "es lista"}}]},
        {"messages": [{"tool_calls": [{"function": {"name": "x"}}]}]},
        {"messages": [{"tool_calls": ["no-es-dict"]}]},
    ):
        assert hook._normalize_tool_call_arguments(data) == 0


def test_avisa_cuando_reescribe(hook, caplog):
    """El log ES la atribucion del origen: sin el no se sabe quien manda argumentos rotos.

    Importa que sea WARNING: el proceso proxy filtra INFO (gotcha ya documentado de
    session_router), asi que un INFO aqui seria un arreglo sin evidencia.
    """
    with caplog.at_level(logging.WARNING, logger="test.hook.arguments"):
        data = _data_with("no-es-json")
        hook._normalize_tool_call_arguments(data)
    assert any("tool_arguments" in r.getMessage() for r in caplog.records), (
        "reescribe en silencio: no habra forma de saber quien manda el veneno"
    )


def test_el_hook_llama_al_helper():
    """Un helper puro que nadie inviste no arregla nada: es el fallo clasico del embed.

    Se comprueba que la llamada esta DENTRO de async_pre_call_hook, no en otro sitio
    que no corre por request.
    """
    tree = ast.parse(_hook_source())

    def find(node, name):
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return n
        return None

    hook_fn = find(tree, "async_pre_call_hook")
    assert hook_fn is not None, "el hook ya no tiene async_pre_call_hook"
    called = {
        n.func.id for n in ast.walk(hook_fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_normalize_tool_call_arguments" in called, (
        "_normalize_tool_call_arguments no se llama desde async_pre_call_hook"
    )
