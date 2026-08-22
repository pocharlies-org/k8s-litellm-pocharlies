"""Contract for the single dynamic local capability: ``tooling``.

The capability follows the resident that is operational according to the GPU
arbiter's component readiness. It has no cloud or cross-profile fallback.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
WANT_FN = {
    "_component_is_ready",
    "_ready_tooling_modes",
    "_select_ready_tooling_mode",
    "_tooling_target_for_compute_mode",
    "_tooling_route_for_state",
}
WANT_CONST = {
    "TOOLING_MODE_TARGETS",
    "TOOLING_MODE_COMPONENTS",
    "TOOLING_FALLBACKS",
}


@pytest.fixture(scope="module")
def hook():
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    source = next(
        doc["data"]["litellm_strip_params.py"]
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )
    keep = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FN:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") in WANT_CONST for target in node.targets
        ):
            keep.append(node)
    module = types.ModuleType("tooling_profile")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), module.__dict__)
    return module


def _state(*, deepseek=False, qwen=False, desired="llm-tp", effective="llm-tp", phase="ready"):
    component = lambda name, ready: {
        "name": name,
        "ready": ready,
        "desired_replicas": 1 if ready else 0,
        "ready_replicas": 1 if ready else 0,
    }
    return {
        "phase": phase,
        "desired_mode": desired,
        "effective_mode": effective,
        "components": {
            "dgx1": [
                component("deepseek-worker", deepseek),
                component("dense-uncensored", qwen),
            ],
            "dgx2": [component("deepseek-head", deepseek)],
        },
    }


def test_tooling_uses_deepseek_only_when_both_ranks_are_ready(hook):
    state = _state(deepseek=True)
    assert hook._tooling_target_for_compute_mode(state) == (
        "deepseek-v4-flash-0731",
        None,
    )
    assert hook._tooling_route_for_state(state, lambda name: name == "deepseek-v4-flash-0731") == (
        "deepseek-v4-flash-0731",
        "primary",
        None,
    )


def test_tooling_uses_qwen_when_qwen_is_the_ready_resident(hook):
    state = _state(qwen=True, desired="creative", effective="creative")
    assert hook._tooling_target_for_compute_mode(state) == ("qwen38-27b", None)
    assert hook._tooling_route_for_state(state, lambda name: name == "qwen38-27b") == (
        "qwen38-27b",
        "primary",
        None,
    )


def test_transition_keeps_whichever_resident_is_actually_ready(hook):
    state = _state(
        qwen=True,
        desired="llm-tp",
        effective="creative",
        phase="switching",
    )
    assert hook._tooling_target_for_compute_mode(state) == ("qwen38-27b", None)


def test_no_ready_resident_fails_closed_without_fallback(hook):
    state = _state()
    assert hook.TOOLING_FALLBACKS == ()
    assert hook._tooling_route_for_state(state, lambda _name: False) == (
        None,
        "dry",
        "tooling_resident_not_ready",
    )


def test_proxy_fallbacks_never_leave_local_models():
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    raw = next(
        doc["data"]["config.yaml"]
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )
    config = yaml.safe_load(raw)
    entries = config.get("router_settings", {}).get("fallbacks") or []
    graph = {
        source: destinations
        for entry in entries
        for source, destinations in entry.items()
    }
    assert graph == {
        "high": ["deepseek-v4-flash-0731"],
        "max": ["deepseek-v4-flash-0731"],
    }
    assert all("/" not in target for targets in graph.values() for target in targets)
