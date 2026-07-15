import asyncio
import logging
import os
from pathlib import Path

import pytest
import yaml

litellm = pytest.importorskip("litellm")
Router = litellm.Router


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
QWEN_MODEL_ID = "dgx2-qwen36-27b-dense-nvfp4-compaction-local"
ORNITH_MODEL_ID = "dgx1-ornith-35b-nvfp4-mtp-256k-tooling"

logging.getLogger("LiteLLM Router").setLevel(logging.CRITICAL)


def router_settings():
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    config_map = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == "litellm-config"
    )
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    return config["router_settings"]


def model(model_name, model_id, mock_response):
    return {
        "model_name": model_name,
        "litellm_params": {
            "model": f"openai/{model_name}",
            "api_key": "unit-test",
            "mock_response": mock_response,
            "num_retries": 0,
        },
        "model_info": {
            "id": model_id,
            "max_input_tokens": 262144,
            "context_window": 262144,
        },
    }


def make_router(primary_response="qwen-ok", include_primary=True):
    settings = router_settings()
    model_list = []
    if include_primary:
        model_list.append(model("compaction-local", QWEN_MODEL_ID, primary_response))
    model_list.append(model("tooling", ORNITH_MODEL_ID, "ornith-ok"))
    return Router(
        model_list=model_list,
        fallbacks=settings["fallbacks"],
        max_fallbacks=settings["max_fallbacks"],
        enable_pre_call_checks=settings["enable_pre_call_checks"],
        num_retries=settings["num_retries"],
    )


def run_completion(router, requested_model="compaction-local", **kwargs):
    async def complete_and_flush_callbacks():
        if requested_model == "compaction-local":
            # Exact output of the proxy hook: override the global retry policy
            # so one primary attempt is followed by at most one fallback.
            kwargs.setdefault("num_retries", 0)
            kwargs.setdefault("timeout", 249)
        response = await router.acompletion(
            model=requested_model,
            messages=[{"role": "user", "content": "ping"}],
            **kwargs,
        )
        # LiteLLM dispatches response logging on its async worker. Let that
        # worker drain before asyncio.run closes the per-test event loop.
        await asyncio.sleep(0.05)
        return response

    return asyncio.run(complete_and_flush_callbacks())


def assert_model_id(response, expected_id, expected_fallbacks):
    hidden = response._hidden_params
    headers = hidden["additional_headers"]
    # The proxy exposes hidden model_id as response header x-litellm-model-id.
    assert hidden["model_id"] == expected_id
    assert headers["x-litellm-attempted-retries"] == 0
    assert headers["x-litellm-attempted-fallbacks"] == expected_fallbacks


def test_normal_request_uses_qwen_dgx2_primary():
    response = run_completion(make_router())
    assert_model_id(response, QWEN_MODEL_ID, 0)


def test_mock_testing_fallbacks_returns_exact_ornith_model_id_once():
    response = run_completion(make_router(), mock_testing_fallbacks=True)
    assert_model_id(response, ORNITH_MODEL_ID, 1)


def test_zero_primary_endpoints_preserves_group_and_falls_back_once():
    response = run_completion(make_router(include_primary=False))
    assert_model_id(response, ORNITH_MODEL_ID, 1)


class CopyableServiceUnavailable(litellm.ServiceUnavailableError):
    def __deepcopy__(self, memo):
        return self


class CopyableTimeout(litellm.Timeout):
    def __deepcopy__(self, memo):
        return self


@pytest.mark.parametrize(
    "primary_error",
    [
        CopyableServiceUnavailable("unit 503", "openai", "compaction-local"),
        CopyableTimeout("unit timeout", "compaction-local", "openai"),
    ],
    ids=["503", "timeout"],
)
def test_retryable_primary_failure_falls_back_to_ornith_once(primary_error):
    response = run_completion(make_router(primary_response=primary_error))
    assert_model_id(response, ORNITH_MODEL_ID, 1)


def test_over_262144_input_tokens_is_rejected_before_inference():
    router = make_router()
    with pytest.raises(litellm.ContextWindowExceededError):
        asyncio.run(
            router.acompletion(
                model="compaction-local",
                messages=[{"role": "user", "content": " token" * 270000}],
                num_retries=0,
                timeout=249,
            )
        )


def test_tooling_has_no_inverse_fallback_to_qwen():
    response = run_completion(make_router(), requested_model="tooling")
    assert_model_id(response, ORNITH_MODEL_ID, 0)


def embedded_hook_namespace(tmp_path):
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    config_map = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == "litellm-config"
    )
    os.environ["LITELLM_ACTIVE_FILE"] = str(tmp_path / "active.json")
    namespace = {"__name__": "litellm_strip_params_contract"}
    exec(compile(config_map["data"]["litellm_strip_params.py"], "litellm_strip_params.py", "exec"), namespace)
    return namespace


def test_hook_preserves_compaction_name_serializes_tools_and_guards_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPACTION_LOCAL_ATTEMPT_TIMEOUT_SECONDS", "249")
    monkeypatch.setenv("COMPACTION_LOCAL_MARGIN_SECONDS", "100")
    hook = embedded_hook_namespace(tmp_path)["proxy_handler_instance"]
    data = {
        "model": "compaction-local",
        "messages": [{"role": "user", "content": "compact"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "litellm_params": {},
    }

    result = asyncio.run(hook.async_pre_call_hook({}, None, data, "completion"))

    assert result["model"] == "compaction-local"
    assert result["parallel_tool_calls"] is False
    assert result["num_retries"] == 0
    assert result["timeout"] == 249

    sync_data = {
        "model": "tooling",
        "messages": [],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "litellm_params": {},
    }
    assert hook.pre_call_hook({}, None, sync_data, "completion")["parallel_tool_calls"] is False


def test_hook_blocks_compaction_when_budget_is_unset_or_not_strictly_under_600(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COMPACTION_LOCAL_ATTEMPT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("COMPACTION_LOCAL_MARGIN_SECONDS", raising=False)
    hook = embedded_hook_namespace(tmp_path)["proxy_handler_instance"]
    data = {"model": "compaction-local", "messages": [], "litellm_params": {}}
    with pytest.raises(ValueError, match="blocked until benchmarked"):
        asyncio.run(hook.async_pre_call_hook({}, None, data, "completion"))

    monkeypatch.setenv("COMPACTION_LOCAL_ATTEMPT_TIMEOUT_SECONDS", "250")
    monkeypatch.setenv("COMPACTION_LOCAL_MARGIN_SECONDS", "100")
    with pytest.raises(ValueError, match=r"2\*attempt_timeout \+ margin"):
        asyncio.run(hook.async_pre_call_hook({}, None, data, "completion"))
