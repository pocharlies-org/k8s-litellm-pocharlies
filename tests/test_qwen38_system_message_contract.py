"""Contrato: los `system` no iniciales se normalizan para qwen38-flash-next.

El chat template de qwen38-flash-next rechaza con HTTP 400 ("System message
must be at the beginning") cualquier mensaje `system` que no vaya al
principio. opencode inyecta system-reminders a MITAD de conversacion, asi que
sin esta normalizacion el turno entero muere. Se carga el hook REAL del
manifest y se ejecuta solo la funcion pura, con el mismo patron que
test_family_sampling_contract.
"""

import ast
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_normalize_leading_system_messages"}
WANT_CONST = {"STRICT_LEADING_SYSTEM_MODELS"}


@pytest.fixture(scope="module")
def hook():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(
        d["data"]["litellm_strip_params.py"]
        for d in docs
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"] == "litellm-config"
    )
    tree = ast.parse(src)
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (
            isinstance(n, ast.Assign)
            and any(
                getattr(t, "id", None) in WANT_CONST for t in n.targets
            )
        )
    ]
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), ns)
    missing = (WANT_FN | WANT_CONST) - set(ns)
    assert not missing, f"faltan en el hook: {missing}"
    return ns


def test_el_modelo_estricto_esta_declarado(hook):
    assert "qwen38-flash-next" in hook["STRICT_LEADING_SYSTEM_MODELS"]


def test_conversacion_sin_system_intermedios_queda_intacta(hook):
    msgs = [
        {"role": "system", "content": "eres util"},
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "dime"},
        {"role": "user", "content": "sigue"},
    ]
    data = {"messages": [dict(m) for m in msgs]}
    hook["_normalize_leading_system_messages"](data)
    assert data["messages"] == msgs


def test_el_bloque_inicial_de_system_se_conserva(hook):
    data = {
        "messages": [
            {"role": "system", "content": "a"},
            {"role": "system", "content": "b"},
            {"role": "user", "content": "hola"},
        ]
    }
    hook["_normalize_leading_system_messages"](data)
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["system", "system", "user"]


def test_system_intermedio_pasa_a_user_etiquetado_y_en_orden(hook):
    data = {
        "messages": [
            {"role": "system", "content": "inicial"},
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "dime"},
            {"role": "system", "content": "recordatorio"},
            {"role": "user", "content": "sigue"},
        ]
    }
    hook["_normalize_leading_system_messages"](data)
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["system", "user", "assistant", "user", "user"]
    reminder = data["messages"][3]
    assert reminder["content"].startswith("<system_reminder>")
    assert "recordatorio" in reminder["content"]
    assert reminder["content"].endswith("</system_reminder>")
    # El resto de mensajes no se tocan.
    assert data["messages"][4] == {"role": "user", "content": "sigue"}


def test_contenido_en_lista_conserva_las_partes(hook):
    parts = [{"type": "text", "text": "recordatorio"}]
    data = {
        "messages": [
            {"role": "user", "content": "hola"},
            {"role": "system", "content": list(parts)},
        ]
    }
    hook["_normalize_leading_system_messages"](data)
    m = data["messages"][1]
    assert m["role"] == "user"
    assert m["content"][0] == {"type": "text", "text": "<system_reminder>"}
    assert m["content"][1] == parts[0]
    assert m["content"][-1] == {"type": "text", "text": "</system_reminder>"}


def test_payload_sin_messages_no_revienta(hook):
    data = {"input": "hola"}
    hook["_normalize_leading_system_messages"](data)
    assert data == {"input": "hola"}
