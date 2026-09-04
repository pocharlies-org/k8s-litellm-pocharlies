"""Aurora keeps the local tooling resident and fails closed without it.

There are two independent failure paths to cover:

* missing local alias: the pre-call hook must not invent another route;
* registered but unhealthy local deployment: LiteLLM's Router otherwise follows
  ``router_settings.fallbacks`` after the hook returns.

The first is controlled while resolving the tooling profile. The second is the
top-level ``disable_fallbacks`` kwarg consumed by LiteLLM v1.96.0's Router. Other
keys retain the shared, availability-oriented fallback graph.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {
    "_apply_key_fallback_policy",
    "_auth_field",
    "_component_is_ready",
    "_compute_mode_allows_local",
    "_ready_tooling_modes",
    "_resolve_key_label",
    "_select_ready_tooling_mode",
    "_tooling_mode_for_model",
    "_tooling_route_for_state",
    "_tooling_target_for_compute_mode",
}
WANT_CONST = {
    "NO_FALLBACK_KEY_ALIASES",
    "TOOLING_FALLBACKS",
    "TOOLING_MODE_COMPONENTS",
    "TOOLING_MODE_TARGETS",
    "TOOLING_PROFILE_ALIASES",
    "TOOLING_UNCENSORED_ALIASES",
    "TOOLING_UNCENSORED_MODE_TARGETS",
}


def _hook_source():
    return next(
        doc["data"]["litellm_strip_params.py"]
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc
        and doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def hook():
    tree = ast.parse(_hook_source())
    keep = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in WANT_FN)
        or (
            isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") in WANT_CONST for target in node.targets)
        )
    ]
    missing = WANT_FN - {
        node.name for node in keep if isinstance(node, ast.FunctionDef)
    }
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    module = types.ModuleType("aurora_fallback_policy")
    exec(
        compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"),
        module.__dict__,
    )
    return module


READY_TP = {
    "phase": "ready",
    "desired_mode": "llm-tp",
    "effective_mode": "llm-tp",
    "components": {
        "dgx1": [{"name": "deepseek-worker", "ready": True,
                  "desired_replicas": 1, "ready_replicas": 1}],
        "dgx2": [{"name": "deepseek-head", "ready": True,
                  "desired_replicas": 1, "ready_replicas": 1}],
    },
}


def _policy(hook, key_alias, data=None):
    request = {} if data is None else data
    disabled = hook._apply_key_fallback_policy(
        request, {"key_alias": key_alias}
    )
    return request, disabled


def test_aurora_uses_the_healthy_local_resident(hook):
    request, disabled = _policy(hook, "aurora-rca")
    live = lambda alias: alias == "qwen38-flash-next"

    assert disabled is True
    assert request["disable_fallbacks"] is True
    assert hook._tooling_route_for_state(
        READY_TP, live, disable_fallbacks=disabled
    ) == ("qwen38-flash-next", "primary", None)


def test_aurora_falla_cerrado_si_el_alias_local_no_esta(hook):
    _, disabled = _policy(hook, "aurora-rca")
    nada_vivo = lambda alias: False

    assert hook._tooling_route_for_state(
        READY_TP, nada_vivo, disable_fallbacks=disabled
    ) == (None, "dry", "compute_profile_target_unavailable")


def test_registered_but_unhealthy_keeps_the_router_kill_switch(hook):
    """The hook sees registration, not backend health; the Router sees health.

    Returning the local alias together with the top-level flag is therefore the
    exact hand-off needed for LiteLLM to raise the local error instead of following
    any global fallback edge.
    """
    request, disabled = _policy(
        hook, "aurora-rca", {"disable_fallbacks": False}
    )
    registered_but_unhealthy = lambda alias: alias == "qwen38-flash-next"

    assert hook._tooling_route_for_state(
        READY_TP, registered_but_unhealthy, disable_fallbacks=disabled
    ) == ("qwen38-flash-next", "primary", None)
    assert request == {"disable_fallbacks": True}


@pytest.mark.parametrize("key_alias", ["keep", "k8sgpt", "openclaw", "unknown"])
def test_las_demas_keys_tampoco_tienen_salida_externa(hook, key_alias):
    """La politica de key SIGUE distinguiendo a Aurora (falla cerrado) del resto -- por
    eso se conservan las aserciones sobre `disabled` y `disable_fallbacks`. Lo que
    desaparece es el salto que esa politica deshabilitaba: sin destino independiente
    las dos ramas acaban igual, en `dry`.
    """
    request, disabled = _policy(hook, key_alias)
    nada_vivo = lambda alias: False

    assert disabled is False
    assert "disable_fallbacks" not in request
    assert hook._tooling_route_for_state(
        READY_TP, nada_vivo, disable_fallbacks=disabled
    ) == (None, "dry", "compute_profile_target_unavailable")


def test_policy_is_wired_before_tooling_resolution():
    source = _hook_source()
    tree = ast.parse(source)
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_pre_call_hook"
    )
    policy_call = next(
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_apply_key_fallback_policy"
    )
    resolution_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_resolve_tooling_profile"
    ]

    assert len(resolution_calls) == 1
    for call in resolution_calls:
        keyword = next(
            (item for item in call.keywords if item.arg == "disable_fallbacks"),
            None,
        )
        assert keyword is not None
        assert isinstance(keyword.value, ast.Name)
        assert keyword.value.id == "fallbacks_disabled"
        assert policy_call.lineno < call.lineno


def test_global_tooling_fallback_remains_for_every_other_key():
    config = next(
        yaml.safe_load(doc["data"]["config.yaml"])
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc
        and doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )
    # 24-08-2026: la clave `fallbacks` ya no existe. Sus dos unicas entradas eran
    # `high` y `max`, que se retiran del model_list por no ser modelos. La regla
    # que este test protege no cambia y ahora se cumple de la forma mas fuerte
    # posible: si no hay grafo, `tooling` no puede tener salto.
    graph = {
        source: destinations
        for edge in config["router_settings"].get("fallbacks") or []
        for source, destinations in edge.items()
    }

    assert "tooling" not in graph, "tooling debe fallar si no hay residente local"
