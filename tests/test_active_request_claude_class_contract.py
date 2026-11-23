"""Contrato (dgx.litellm.active-requests.v1, campo aditivo `claude_class`).

Las sesiones de la compania piden Claude con `claude-atlantic`, la misma del Paseo,
asi que la key ya no las distingue (DGX-1794). El wrapper estampa
`x-claude-class: company` (dgx.claude.class-header.v1); el hook la lee, el tracker
la guarda en la fila en vuelo y el sidecar /internal/active-requests la sirve, para
que el panel de dgx-infra separe compania/Paseo por clase y no por alias. El codigo
VIVE en el ConfigMap.
"""
import ast
import tempfile
import time
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


def test_el_tracker_guarda_la_clase_en_la_fila():
    module = _exec_module(_config_data()["active_request_tracking.py"], "cc_contract")
    with tempfile.TemporaryDirectory() as d:
        tracker = module["ActiveRequestTracker"](str(Path(d) / "active.json"))
        tracker.start("r1", key_alias="claude-atlantic", model="qwen38-flash-next",
                      call_type="acompletion", api_base=None, claude_class="Company")
        tracker.start("r2", key_alias="claude-atlantic", model="qwen38-flash-next",
                      call_type="acompletion", api_base=None)
        rows = tracker.snapshot()
        # Normalizada a minusculas: el wrapper manda "company"; el panel compara con "company".
        assert rows["r1"]["claude_class"] == "company"
        # Paseo (misma key, sin cabecera): None, no "company". Es justo lo que distingue.
        assert rows["r2"]["claude_class"] is None


def test_el_sidecar_sirve_la_clase():
    module = _exec_module(_config_data()["active_requests_api.py"], "cc_sidecar")
    rows = module["_normalize"]({"active": [
        {"request_id": "r1", "alias": "claude-atlantic", "claude_class": "company", "ts": time.time()},
        {"request_id": "r2", "alias": "claude-atlantic", "ts": time.time()},
    ]})
    by_id = {r["request_id"]: r for r in rows}
    assert by_id["r1"]["claude_class"] == "company"
    assert by_id["r2"]["claude_class"] is None


def test_el_hook_pasa_la_clase_al_tracker():
    """El start() del hook tiene que llevar claude_class=; si no, el panel nunca la ve en vuelo."""
    tree = ast.parse(_hook_source())
    starts = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "start"
              and getattr(n.func.value, "id", "") == "request_tracker"]
    assert starts, "el hook ya no llama a request_tracker.start(...)"
    for call in starts:
        kw = {k.arg for k in call.keywords}
        assert "claude_class" in kw, (
            "request_tracker.start(...) perdio el kwarg claude_class: "
            "la compania volveria a salir mezclada con Paseo en el panel en vivo"
        )
