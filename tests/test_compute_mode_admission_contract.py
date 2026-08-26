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
    functions = {
        "_component_is_ready",
        "_ready_tooling_modes",
        "_tooling_mode_for_model",
        "_compute_mode_allows_local",
    }
    constants = {
        "TOOLING_MODE_TARGETS",
        "TOOLING_MODE_COMPONENTS",
        "TOOLING_UNCENSORED_MODE_TARGETS",
        "TOOLING_PROFILE_ALIASES",
        "TOOLING_UNCENSORED_ALIASES",
    }
    keep = [
        node for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in functions)
        or (isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") in constants for target in node.targets))
    ]
    module = types.ModuleType("compute_mode_gate")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<gate>", "exec"), module.__dict__)
    return module._compute_mode_allows_local


def test_tooling_admission_follows_the_ready_resident_during_transitions():
    gate = _pure_gate()
    qwen_ready = {
        "phase": "waiting",
        "desired_mode": "llm-tp",
        "effective_mode": "creative",
        "components": {
            "dgx1": [{"name": "dense-uncensored", "ready": True,
                      "desired_replicas": 1, "ready_replicas": 1}],
        },
    }
    assert gate(None, "tooling") == (False, "compute_mode_unavailable")
    assert gate({"phase": "waiting"}, "tooling") == (
        False, "tooling_resident_not_ready"
    )
    assert gate(qwen_ready, "tooling") == (True, None)
    assert gate(qwen_ready, "qwen38-27b") == (True, None)
    # 26-08: el nombre directo del residente llm-tp es qwen38-flash-next.
    assert gate(qwen_ready, "qwen38-flash-next") == (
        False, "tooling_resident_not_ready"
    )
    # Fuera de los residentes/tooling se mantiene la puerta historica —
    # incluido el nombre directo de DeepSeek, que ya no es residente de perfil.
    assert gate(qwen_ready, "deepseek-v4-flash-0731") == (
        False, "compute_mode_transition"
    )
    assert gate(qwen_ready, "ornith-1.0") == (False, "compute_mode_transition")


def test_hook_checks_mode_before_tracking_a_new_local_request():
    source = _hook_source()
    gate = source.index("await _enforce_compute_mode_admission")
    tracking = source.index("request_tracker.start(", gate)
    assert gate < tracking
    assert "COMPUTE_MODE_CACHE_SECONDS" in source
    assert 'os.environ.get("COMPUTE_MODE_CACHE_SECONDS", "5")' in source
    assert 'os.environ.get("COMPUTE_MODE_TIMEOUT_SECONDS", "10")' in source
    assert "httpx.AsyncClient(timeout=COMPUTE_MODE_TIMEOUT_SECONDS)" in source
    assert "COMPUTE_MODE_RETRY_AFTER_SECONDS" in source
    assert "_compute_mode_allows_local(state, model)" in source
    assert '"deepseek-worker"' in source
    assert '"deepseek-head"' in source
    assert '"dense-uncensored"' in source
    assert 'status_code=503' in source
    assert 'headers={"Retry-After": str(COMPUTE_MODE_RETRY_AFTER_SECONDS)}' in source


def test_litellm_pod_rolls_and_points_at_typed_compute_mode_get():
    """El pod tiene que rodar con el hook, y apuntar al GET tipado del modo.

    2026-08-11: la primera mitad pinneaba el literal `compute-mode-admission-20260810`.
    Eso no es el contrato -- ese valor ES el marcador de rollout, y todo cambio del
    hook lo cambia por definicion (aqui lo cambio el desvio de vision). Lo que
    importa es que la anotacion siga estando en el pod template: sin ella el hook
    se sincroniza y el pod no rueda.
    """
    text = MANIFEST.read_text()
    doc = next(d for d in yaml.safe_load_all(text)
               if d and d.get("kind") == "Deployment"
               and d["metadata"]["name"] == "litellm")
    anotaciones = doc["spec"]["template"]["metadata"]["annotations"]
    assert anotaciones.get("config.k8s.e-dani.com/revision")
    assert (
        "http://dgx-dashboard-backend.control-nexus.svc.cluster.local:9002"
        "/api/compute/mode"
    ) in text
    assert (
        '{ name: COMPUTE_MODE_RETRY_AFTER_SECONDS, value: "5" }'
        in text
    )
    assert '{ name: COMPUTE_MODE_CACHE_SECONDS, value: "5" }' in text
    assert '{ name: COMPUTE_MODE_TIMEOUT_SECONDS, value: "10" }' in text
