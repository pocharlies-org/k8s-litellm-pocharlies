"""Contrato dgx.litellm.spendlogs-session-title.v1: el hook sella
`spend_logs_metadata.session_title` con la etiqueta de `x-claude-session-title`
(dgx.claude.session-title-header.v1).

Lo lee dgx-infra (SessionRouting: nombre de sesion cuando el indice de titulos
del x86 no conoce el sid) por SQL sobre LiteLLM_SpendLogs. El codigo probado
VIVE en el ConfigMap; se extrae como en
test_spendlogs_claude_class_contract.py.
"""
import ast
import re
import types

import pytest

# El extractor del ConfigMap ya vive en el test vecino (patrón de toda la suite); se
# importa en vez de copiarlo — el check `duplicados` lo exige.
from test_spendlogs_claude_class_contract import _hook_source


@pytest.fixture(scope="module")
def hook():
    arbol = ast.parse(_hook_source())
    cuerpo = [n for n in arbol.body if isinstance(n, ast.FunctionDef)
              and n.name in ("_session_title_header", "_stamp_session_title")]
    assert len(cuerpo) == 2, "el hook ya no define las dos funciones del titulo"
    mod = types.ModuleType("hooktitle")
    mod.__dict__["re"] = re
    exec(compile(ast.Module(body=cuerpo, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


def test_sella_en_metadata(hook):
    data = {"metadata": {"user_api_key_alias": "hermes"}}
    assert hook._stamp_session_title(data, "SC-2956, D3a: revisores") is True
    assert data["metadata"]["spend_logs_metadata"] == {"session_title": "SC-2956, D3a: revisores"}


def test_v1_messages_va_a_litellm_metadata(hook):
    data = {"metadata": {"user_id": "x"}, "litellm_metadata": {}}
    hook._stamp_session_title(data, "titulo")
    assert data["litellm_metadata"]["spend_logs_metadata"]["session_title"] == "titulo"
    assert "spend_logs_metadata" not in data["metadata"]


def test_convive_con_los_sellos_de_clase(hook):
    data = {"metadata": {"spend_logs_metadata": {"claude_class": "company"}}}
    hook._stamp_session_title(data, "titulo")
    assert data["metadata"]["spend_logs_metadata"] == {"claude_class": "company", "session_title": "titulo"}


@pytest.mark.parametrize("titulo", [None, ""])
def test_sin_titulo_no_escribe(hook, titulo):
    data = {"metadata": {}}
    assert hook._stamp_session_title(data, titulo) is False
    assert data == {"metadata": {}}


@pytest.mark.parametrize(
    "data",
    [
        {"headers": {"x-claude-session-title": "mi sesion"}},
        {"metadata": {"headers": {"X-Claude-Session-Title": "mi sesion"}}},
        {"litellm_metadata": {"headers": {"x-claude-session-title": "mi sesion"}}},
    ],
)
def test_lee_de_las_tres_fuentes(hook, data):
    assert hook._session_title_header(data) == "mi sesion"


def test_colapsa_saltos_y_recorta(hook):
    data = {"metadata": {"headers": {"x-claude-session-title": "a\nb   c\t" + "x" * 200}}}
    v = hook._session_title_header(data)
    assert v.startswith("a b c ")
    assert len(v) == 120
    assert "\n" not in v


@pytest.mark.parametrize(
    "data",
    [
        {"metadata": {}},
        {"metadata": {"headers": {"x-claude-session-title": "   "}}},
        {"metadata": {"headers": "no-es-dict"}},
        "no-es-dict",
    ],
)
def test_sin_cabecera_o_forma_rara_none(hook, data):
    assert hook._session_title_header(data) is None


def test_sin_titulo_no_escribe_cabecera_none(hook):
    # La via completa: sin cabecera util el sello no escribe nada.
    data = {"metadata": {}}
    assert hook._stamp_session_title(data, hook._session_title_header({"metadata": {}})) is False
    assert data == {"metadata": {}}
