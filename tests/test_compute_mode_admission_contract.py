import ast
import types
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _hook_source():
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    return next(
        doc["data"]["litellm_strip_params.py"]
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )


def _pure_gate():
    tree = ast.parse(_hook_source())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_compute_mode_allows_local"
    )
    module = types.ModuleType("compute_mode_gate")
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<gate>", "exec"), module.__dict__)
    return module._compute_mode_allows_local


def test_local_llm_admission_is_closed_until_one_profile_is_fully_ready():
    gate = _pure_gate()
    assert gate(None) == (False, "compute_mode_unavailable")
    assert gate({"phase": "waiting"}) == (False, "compute_mode_transition")
    assert gate({
        "phase": "ready", "desired_mode": "creative", "effective_mode": "llm-tp"
    }) == (False, "compute_mode_inconsistent")
    assert gate({
        "phase": "ready", "desired_mode": "creative", "effective_mode": "creative"
    }) == (True, None)


def test_hook_checks_mode_before_tracking_a_new_local_request():
    source = _hook_source()
    gate = source.index("await _enforce_compute_mode_admission")
    tracking = source.index("request_tracker.start(", gate)
    assert gate < tracking
    assert "COMPUTE_MODE_CACHE_SECONDS" in source
    assert 'status_code=503' in source


def test_litellm_pod_rolls_and_points_at_typed_compute_mode_get():
    text = MANIFEST.read_text()
    assert "compute-mode-admission-20260810" in text
    assert (
        "http://dgx-dashboard-backend.control-nexus.svc.cluster.local:9002"
        "/api/compute/mode"
    ) in text
