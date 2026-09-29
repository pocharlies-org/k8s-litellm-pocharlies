"""Contrato dgx.litellm.spendlogs-claude-class.v1: el hook sella
`spend_logs_metadata.claude_class` con la clase de `x-claude-class`.

Lo lee dgx-infra (uso de Alibaba por consumidor y presupuesto diario de la
compania) por SQL sobre LiteLLM_SpendLogs. El codigo probado VIVE en el
ConfigMap; se extrae como en test_refusal_probe_stamp_contract.py.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in (x for x in yaml.safe_load_all(MANIFEST.read_text()) if x)
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def stamp():
    tree = ast.parse(_hook_source())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_stamp_claude_class"]
    assert keep, "el hook ya no define _stamp_claude_class"
    mod = types.ModuleType("hookclass")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod._stamp_claude_class


def test_sella_en_metadata(stamp):
    data = {"metadata": {"user_api_key_alias": "hermes"}}
    assert stamp(data, "company") is True
    assert data["metadata"]["spend_logs_metadata"] == {"claude_class": "company"}


def test_v1_messages_va_a_litellm_metadata(stamp):
    data = {"metadata": {"user_id": "x"}, "litellm_metadata": {}}
    stamp(data, "Company ")
    assert data["litellm_metadata"]["spend_logs_metadata"]["claude_class"] == "company"
    assert "spend_logs_metadata" not in data["metadata"]


def test_convive_con_el_sello_de_lambda(stamp):
    data = {"metadata": {"spend_logs_metadata": {"refusal_lambda": "1.0"}}}
    stamp(data, "company")
    assert data["metadata"]["spend_logs_metadata"] == {"refusal_lambda": "1.0", "claude_class": "company"}


@pytest.mark.parametrize("cls", [None, ""])
def test_sin_clase_no_escribe(stamp, cls):
    data = {"metadata": {}}
    assert stamp(data, cls) is False
    assert data == {"metadata": {}}


def test_el_hook_lo_llama_con_la_clase_del_router():
    src = _hook_source()
    assert "_stamp_claude_class(" in src
    assert "session_router._claude_class(data)" in src
