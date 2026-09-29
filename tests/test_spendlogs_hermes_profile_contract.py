"""Contrato dgx.litellm.spendlogs-hermes-profile.v1: el hook sella
`spend_logs_metadata.hermes_profile` con el perfil de `x-hermes-profile`.

Lo lee dgx-infra (uso por perfil y consumidor en Inferencia) por SQL sobre
LiteLLM_SpendLogs. El codigo probado VIVE en el ConfigMap.
"""
import ast
import re
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
def mod():
    tree = ast.parse(_hook_source())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in ("_stamp_hermes_profile", "_hermes_profile"))
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_PROFILE_RE" for t in n.targets))]
    assert len(keep) == 3, "el hook ya no define _hermes_profile/_stamp_hermes_profile/_PROFILE_RE"
    m = types.ModuleType("hookprofile")
    m.re = re
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), m.__dict__)  # noqa: S102
    return m


def test_sella_el_perfil_de_la_cabecera(mod):
    data = {"headers": {"X-Hermes-Profile": "CTO"}, "metadata": {}}
    assert mod._stamp_hermes_profile(data, mod._hermes_profile(data)) is True
    assert data["metadata"]["spend_logs_metadata"] == {"hermes_profile": "cto"}


def test_en_messages_usa_litellm_metadata(mod):
    data = {"litellm_metadata": {}}
    mod._stamp_hermes_profile(data, "qa")
    assert data["litellm_metadata"]["spend_logs_metadata"] == {"hermes_profile": "qa"}


def test_convive_con_la_clase(mod):
    data = {"metadata": {"spend_logs_metadata": {"claude_class": "company"}}}
    mod._stamp_hermes_profile(data, "devops")
    assert data["metadata"]["spend_logs_metadata"] == {"claude_class": "company", "hermes_profile": "devops"}


@pytest.mark.parametrize("headers", [{}, {"x-hermes-profile": "no vale; drop"}])
def test_sin_perfil_valido_no_escribe(mod, headers):
    data = {"headers": headers, "metadata": {}}
    assert mod._stamp_hermes_profile(data, mod._hermes_profile(data)) is False
    assert data == {"headers": headers, "metadata": {}}


def test_el_hook_lo_llama():
    assert "_stamp_hermes_profile(data, _hermes_profile(data))" in _hook_source()
