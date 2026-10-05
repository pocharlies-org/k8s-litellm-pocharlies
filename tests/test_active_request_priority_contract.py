"""Contrato dgx.litellm.active-requests.v1: cada fila en vuelo lleva `priority`.

session_router.apply_priority sella la prioridad de vLLM en extra_body segun
priority_by_alias (solo si el destino es el residente). El hook la pasa al
tracker, el tracker la guarda en la fila y el sidecar /internal/active-requests
la sirve. El panel de dgx-infra ordena con ella la cola estimada del motor
(DGX-589). El codigo VIVE en el ConfigMap.
"""
import tempfile
import time
from pathlib import Path

from test_active_request_metrics_contract import _config_data, _exec_module


def test_el_tracker_guarda_la_prioridad_en_la_fila():
    module = _exec_module(_config_data()["active_request_tracking.py"], "priority_contract")
    with tempfile.TemporaryDirectory() as d:
        tracker = module["ActiveRequestTracker"](str(Path(d) / "active.json"))
        tracker.start("r1", key_alias="hermes-batch", model="qwen38-flash-next", call_type="acompletion",
                      api_base=None, priority=10)
        tracker.start("r2", key_alias="opencode", model="qwen38-flash-next", call_type="acompletion",
                      api_base=None)
        tracker.start("r3", key_alias="x", model="qwen38-flash-next", call_type="acompletion",
                      api_base=None, priority="10")
        tracker.start("r4", key_alias="x", model="qwen38-flash-next", call_type="acompletion",
                      api_base=None, priority=True)
        rows = tracker.snapshot()
        assert rows["r1"]["priority"] == 10
        assert rows["r2"]["priority"] is None
        assert rows["r3"]["priority"] is None
        assert rows["r4"]["priority"] is None


def test_el_hook_pasa_la_prioridad_de_extra_body_al_tracker():
    hook = _config_data()["litellm_strip_params.py"]
    call = hook[hook.index("request_tracker.start("):]
    call = call[:call.index("profile=_hermes_profile(data),") + 400]
    assert 'priority=(data.get("extra_body") or {}).get("priority")' in call


def test_el_sidecar_sirve_la_prioridad():
    module = _exec_module(_config_data()["active_requests_api.py"], "priority_sidecar")
    rows = module["_normalize"]({"active": [
        {"request_id": "r1", "alias": "brain", "priority": 20, "ts": time.time()},
        {"request_id": "r2", "alias": "opencode", "ts": time.time()},
    ]})
    by_id = {r["request_id"]: r for r in rows}
    assert by_id["r1"]["priority"] == 20
    assert by_id["r2"]["priority"] is None
