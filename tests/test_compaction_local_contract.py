import ast
from pathlib import Path
from textwrap import dedent

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def documents():
    return [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]


def resource(kind, name):
    return next(
        doc
        for doc in documents()
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    )


def proxy_config():
    config_map = resource("ConfigMap", "litellm-config")
    return yaml.safe_load(config_map["data"]["config.yaml"])


def sync_namespace():
    config_map = resource("ConfigMap", "litellm-dgx-backend-sync")
    source = dedent(config_map["data"]["sync.py"])
    namespace = {"__name__": "litellm_sync_contract"}
    exec(compile(ast.parse(source), "sync.py", "exec"), namespace)
    return namespace


def test_fallback_graph_is_exact_one_way_and_acyclic():
    settings = proxy_config()["router_settings"]

    assert settings["max_fallbacks"] == 1
    assert settings["enable_pre_call_checks"] is True
    assert settings["fallbacks"] == [{"compaction-local": ["tooling"]}]
    assert "default_fallbacks" not in settings
    assert "context_window_fallbacks" not in settings
    assert "content_policy_fallbacks" not in settings

    graph = {
        source: targets
        for edge in settings["fallbacks"]
        for source, targets in edge.items()
    }
    assert graph == {"compaction-local": ["tooling"]}
    assert "compaction-local" not in graph["compaction-local"]
    assert "tooling" not in graph


def test_compaction_primary_and_tooling_terminal_are_unique_by_alias_backend():
    namespace = sync_namespace()
    backends = namespace["BACKENDS"]

    owners = {
        alias: [backend for backend in backends if alias in backend["aliases"]]
        for alias in ("compaction-local", "tooling")
    }
    assert len(owners["compaction-local"]) == 1
    assert len(owners["tooling"]) == 1

    primary = owners["compaction-local"][0]
    terminal = owners["tooling"][0]
    assert primary["backend"] == "dgx2"
    assert primary["name"] == "qwen36-27b-dense-dgx2"
    assert primary["base_model"].startswith("openai/qwen36-27b-")
    assert terminal["backend"] == "dgx1"
    assert terminal["name"] == "ornith-dgx1"
    assert terminal["base_model"] == "openai/ornith-1.0-35b-nvfp4-mtp"

    pairs = [
        (alias, backend["name"])
        for backend in backends
        for alias in backend["aliases"]
        if alias in owners
    ]
    assert len(pairs) == len(set(pairs)) == 2


def test_compaction_and_tooling_deployment_metadata_is_256k_and_serial():
    namespace = sync_namespace()
    backends = namespace["BACKENDS"]
    desired_deployments = namespace["desired_deployments"]

    primary_backend = next(b for b in backends if "compaction-local" in b["aliases"])
    tooling_backend = next(b for b in backends if "tooling" in b["aliases"])
    primary = next(
        row
        for row in desired_deployments(primary_backend)
        if row["model_name"] == "compaction-local"
    )
    tooling = next(
        row for row in desired_deployments(tooling_backend) if row["model_name"] == "tooling"
    )

    for row in (primary, tooling):
        assert row["model_info"]["max_input_tokens"] == 262144
        assert row["model_info"]["context_window"] == 262144
        assert row["model_info"]["supports_parallel_function_calling"] is False
    assert primary["litellm_params"]["num_retries"] == 0
    assert primary["litellm_params"]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert "extra_body" not in tooling["litellm_params"]


def test_hook_preserves_compaction_name_and_has_no_dense_fallback_rewrite():
    text = MANIFEST.read_text()
    router_block = text[text.index("ROUTER_MODELS ="):text.index("MODEL_MAP =")]
    model_map_block = text[
        text.index("MODEL_MAP ="):text.index("class StripUnsupportedParams")
    ]
    hook_block = text[
        text.index("async def async_pre_call_hook"):text.index("def pre_call_hook")
    ]

    assert '"compaction-local"' not in router_block
    assert '"tooling"' not in router_block
    assert '"fallback"' not in model_map_block
    assert "_alias_has_deployments" not in text
    assert "_apply_compaction_local_policy(data)" in hook_block
    sync_hook = text[text.index("def pre_call_hook"):text.index("async def async_log_success_event")]
    assert "_apply_compaction_local_policy(data)" in sync_hook
    assert "COMPACTION_LOCAL_TEMPERATURE = 0.2" in text
    assert "COMPACTION_LOCAL_TOP_P = 0.8" in text
    assert "COMPACTION_LOCAL_MAX_TOKENS = 16384" in text


def test_proxy_ha_is_explicitly_blocked_until_shared_state_is_safe():
    deployment = resource("Deployment", "litellm")
    annotations = deployment["metadata"]["annotations"]
    all_docs = documents()

    assert deployment["spec"]["replicas"] == 1
    assert annotations["ha.litellm.e-dani.com/status"] == "blocked"
    assert annotations["ha.litellm.e-dani.com/blockers"] == (
        "shared-redis-cooldown,node-local-hostpath-tracking"
    )
    assert not any(doc.get("kind") == "PodDisruptionBudget" for doc in all_docs)
    pod_spec = deployment["spec"]["template"]["spec"]
    assert "affinity" not in pod_spec
    assert "topologySpreadConstraints" not in pod_spec
    tracking = next(volume for volume in pod_spec["volumes"] if volume["name"] == "tracking")
    assert "hostPath" in tracking
