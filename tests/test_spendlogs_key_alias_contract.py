"""Contrato dgx.litellm.spendlogs-key-alias.v1: el hook sella
`spend_logs_metadata.key_alias` con la key que autentico la peticion.

Existe porque la fila del FALLBACK pierde el `user_api_key_alias` que pone
LiteLLM (medido 19-10: 9 filas claude-cli caidas a alibaba-q38-flash sin key en
7 d) y el panel de uso por perfil las pinta `(sin key)`. Lo lee dgx-infra
(services/dashboard/api/uso_perfiles.py) por SQL sobre LiteLLM_SpendLogs.
El codigo probado VIVE en el ConfigMap.
"""
import ast
import types

import pytest

from hook_configmap import hook_source as _hook_source


@pytest.fixture(scope="module")
def mod():
    tree = ast.parse(_hook_source())
    keep = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_stamp_key_alias"]
    assert len(keep) == 1, "el hook ya no define _stamp_key_alias"
    m = types.ModuleType("hookkeyalias")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), m.__dict__)  # noqa: S102
    return m


def test_sella_la_key(mod):
    data = {"metadata": {}}
    assert mod._stamp_key_alias(data, "claude-local") is True
    assert data["metadata"]["spend_logs_metadata"] == {"key_alias": "claude-local"}


def test_en_messages_usa_litellm_metadata(mod):
    data = {"litellm_metadata": {}}
    mod._stamp_key_alias(data, "hermes")
    assert data["litellm_metadata"]["spend_logs_metadata"] == {"key_alias": "hermes"}


def test_convive_con_los_otros_sellos(mod):
    data = {"metadata": {"spend_logs_metadata": {"claude_class": "company", "hermes_profile": "cto"}}}
    mod._stamp_key_alias(data, "hermes-batch")
    assert data["metadata"]["spend_logs_metadata"] == {
        "claude_class": "company", "hermes_profile": "cto", "key_alias": "hermes-batch"}


@pytest.mark.parametrize("alias", [None, "", "unknown"])
def test_sin_key_que_atribuir_no_escribe(mod, alias):
    data = {"metadata": {}}
    assert mod._stamp_key_alias(data, alias) is False
    assert data == {"metadata": {}}


def test_el_hook_lo_llama():
    assert "_stamp_key_alias(data, key_alias)" in _hook_source()
