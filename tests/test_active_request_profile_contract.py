"""Contrato dgx.hermes.profile-header.v1: cada fila en vuelo lleva `profile`.

Todos los perfiles de Hermes comparten la key `hermes`; el seed de Hermes les
pone `x-hermes-profile: <perfil>` y el panel de dgx-infra lo pinta junto al
alias. El hook lo lee de las cabeceras, el tracker lo guarda en la fila y el
sidecar /internal/active-requests lo sirve. El codigo VIVE en el ConfigMap.
"""
import ast
import re
import tempfile
import time
import types
from pathlib import Path

import pytest
import yaml

from test_active_request_metrics_contract import _config_data, _exec_module

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in (x for x in yaml.safe_load_all(MANIFEST.read_text()) if x)
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def perfil():
    tree = ast.parse(_hook_source())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name == "_hermes_profile")
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_PROFILE_RE" for t in n.targets))]
    assert len(keep) == 2, "el hook ya no define _hermes_profile/_PROFILE_RE"
    mod = types.ModuleType("hookprofile")
    mod.re = re
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod._hermes_profile


def test_lee_la_cabecera_de_metadata(perfil):
    assert perfil({"metadata": {"headers": {"x-hermes-profile": "cto"}}}) == "cto"


def test_v1_messages_y_caja_del_cable(perfil):
    data = {"litellm_metadata": {"headers": {"X-Hermes-Profile": " Tech-Lead "}}}
    assert perfil(data) == "tech-lead"


@pytest.mark.parametrize("data", [
    {}, {"metadata": {}}, {"metadata": {"headers": {"x-claude-class": "company"}}},
    {"metadata": {"headers": {"x-hermes-profile": "<script>"}}},
    {"metadata": {"headers": {"x-hermes-profile": "x" * 80}}},
    {"metadata": "roto"},
])
def test_sin_cabecera_valida_es_none(perfil, data):
    assert perfil(data) is None


def test_el_tracker_guarda_el_perfil_en_la_fila():
    module = _exec_module(_config_data()["active_request_tracking.py"], "profile_contract")
    with tempfile.TemporaryDirectory() as d:
        tracker = module["ActiveRequestTracker"](str(Path(d) / "active.json"))
        tracker.start("r1", key_alias="hermes", model="qwen38-flash-next", call_type="acompletion",
                      api_base=None, profile="cto")
        tracker.start("r2", key_alias="opencode", model="qwen38-flash-next", call_type="acompletion",
                      api_base=None)
        rows = tracker.snapshot()
        assert rows["r1"]["profile"] == "cto"
        assert rows["r2"]["profile"] is None


def test_el_sidecar_sirve_el_perfil():
    module = _exec_module(_config_data()["active_requests_api.py"], "profile_sidecar")
    rows = module["_normalize"]({"active": [{"request_id": "r1", "alias": "hermes", "profile": "qa",
                                               "ts": time.time()}]})
    assert rows and rows[0]["profile"] == "qa"
