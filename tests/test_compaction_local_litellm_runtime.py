import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

litellm = pytest.importorskip("litellm")
Router = litellm.Router


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
QWEN_MODEL_ID = "dgx2-qwen36-27b-dense-nvfp4-compaction-local"
ORNITH_MODEL_ID = "dgx1-ornith-35b-nvfp4-mtp-256k-tooling"

logging.getLogger("LiteLLM Router").setLevel(logging.CRITICAL)
MISSING = object()


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


def model(
    model_name,
    model_id,
    mock_response=MISSING,
    api_base=None,
    upstream_model=None,
    extra_body=None,
):
    litellm_params = {
        "model": upstream_model or f"openai/{model_name}",
        "api_key": "unit-test",
        "num_retries": 0,
    }
    if mock_response is not MISSING:
        litellm_params["mock_response"] = mock_response
    if api_base is not None:
        litellm_params["api_base"] = api_base
    if extra_body is not None:
        litellm_params["extra_body"] = extra_body
    return {
        "model_name": model_name,
        "litellm_params": litellm_params,
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


def router_from_models(model_list):
    settings = router_settings()
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


@pytest.fixture
def openai_payload_server():
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payloads.append(json.loads(self.rfile.read(length)))
            response = {
                "id": "chatcmpl-unit",
                "object": "chat.completion",
                "created": 1,
                "model": "ornith-unit",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", payloads
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def compaction_payload(hook, *, async_hook, max_tokens=99999):
    data = {
        "model": "compaction-local",
        "messages": [{"role": "user", "content": "compact"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "unit test",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "temperature": 1.0,
        "top_p": 1.0,
        "max_tokens": max_tokens,
        "parallel_tool_calls": True,
        "chat_template_kwargs": {"enable_thinking": True},
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
        "litellm_params": {},
    }
    if async_hook:
        return asyncio.run(hook.async_pre_call_hook({}, None, data, "completion"))
    return hook.pre_call_hook({}, None, data, "completion")


def run_policy_payload(router, payload, **extra):
    async def complete_and_flush_callbacks():
        response = await router.acompletion(
            model=payload["model"],
            messages=payload["messages"],
            tools=payload["tools"],
            temperature=payload["temperature"],
            top_p=payload["top_p"],
            max_tokens=payload["max_tokens"],
            parallel_tool_calls=payload["parallel_tool_calls"],
            num_retries=payload["num_retries"],
            timeout=payload["timeout"],
            **extra,
        )
        await asyncio.sleep(0.05)
        return response

    return asyncio.run(complete_and_flush_callbacks())


def test_hooks_enforce_compaction_payload_and_leave_direct_tooling_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPACTION_LOCAL_ATTEMPT_TIMEOUT_SECONDS", "249")
    monkeypatch.setenv("COMPACTION_LOCAL_MARGIN_SECONDS", "100")
    hook = embedded_hook_namespace(tmp_path)["proxy_handler_instance"]
    result = compaction_payload(hook, async_hook=True)

    assert result["model"] == "compaction-local"
    assert result["temperature"] == 0.2
    assert result["top_p"] == 0.8
    assert result["max_tokens"] == 16384
    assert result["parallel_tool_calls"] is False
    assert result["num_retries"] == 0
    assert result["timeout"] == 249
    assert "chat_template_kwargs" not in result
    assert "extra_body" not in result

    sync_result = compaction_payload(hook, async_hook=False, max_tokens=4096)
    assert sync_result["model"] == "compaction-local"
    assert sync_result["temperature"] == 0.2
    assert sync_result["top_p"] == 0.8
    assert sync_result["max_tokens"] == 4096
    assert sync_result["parallel_tool_calls"] is False
    assert sync_result["num_retries"] == 0
    assert sync_result["timeout"] == 249
    assert "chat_template_kwargs" not in sync_result
    assert "extra_body" not in sync_result

    direct_tooling = {
        "model": "tooling",
        "messages": [],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 8192,
        "parallel_tool_calls": True,
        "litellm_params": {},
    }
    direct_result = hook.pre_call_hook({}, None, direct_tooling, "completion")
    assert direct_result["temperature"] == 0.7
    assert direct_result["top_p"] == 0.9
    assert direct_result["max_tokens"] == 8192
    assert direct_result["parallel_tool_calls"] is True
    assert "num_retries" not in direct_result
    assert "timeout" not in direct_result


def test_qwen_primary_receives_real_enforced_payload(
    monkeypatch, tmp_path, openai_payload_server
):
    monkeypatch.setenv("COMPACTION_LOCAL_ATTEMPT_TIMEOUT_SECONDS", "249")
    monkeypatch.setenv("COMPACTION_LOCAL_MARGIN_SECONDS", "100")
    hook = embedded_hook_namespace(tmp_path)["proxy_handler_instance"]
    api_base, captured = openai_payload_server
    router = router_from_models(
        [
            model(
                "compaction-local",
                QWEN_MODEL_ID,
                api_base=api_base,
                upstream_model="openai/qwen36-27b-nvfp4-v024-f2-nvfp4kv",
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            ),
            model("tooling", ORNITH_MODEL_ID, "ornith-ok"),
        ]
    )

    response = run_policy_payload(router, compaction_payload(hook, async_hook=True))

    assert_model_id(response, QWEN_MODEL_ID, 0)
    assert len(captured) == 1
    assert captured[0]["model"] == "qwen36-27b-nvfp4-v024-f2-nvfp4kv"
    assert captured[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured[0]["temperature"] == 0.2
    assert captured[0]["top_p"] == 0.8
    assert captured[0]["max_tokens"] == 16384
    assert captured[0]["parallel_tool_calls"] is False


def test_mock_fallback_ornith_receives_same_real_enforced_payload(
    monkeypatch, tmp_path, openai_payload_server
):
    monkeypatch.setenv("COMPACTION_LOCAL_ATTEMPT_TIMEOUT_SECONDS", "249")
    monkeypatch.setenv("COMPACTION_LOCAL_MARGIN_SECONDS", "100")
    hook = embedded_hook_namespace(tmp_path)["proxy_handler_instance"]
    api_base, captured = openai_payload_server
    router = router_from_models(
        [
            model(
                "compaction-local",
                QWEN_MODEL_ID,
                "qwen-ok",
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            ),
            model(
                "tooling",
                ORNITH_MODEL_ID,
                api_base=api_base,
                upstream_model="openai/ornith-1.0-35b-nvfp4-mtp",
            ),
        ]
    )

    response = run_policy_payload(
        router,
        compaction_payload(hook, async_hook=False),
        mock_testing_fallbacks=True,
    )

    assert_model_id(response, ORNITH_MODEL_ID, 1)
    assert len(captured) == 1
    assert captured[0]["model"] == "ornith-1.0-35b-nvfp4-mtp"
    assert "chat_template_kwargs" not in captured[0]
    assert captured[0]["temperature"] == 0.2
    assert captured[0]["top_p"] == 0.8
    assert captured[0]["max_tokens"] == 16384
    assert captured[0]["parallel_tool_calls"] is False


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
