import ast
import json
import tempfile
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _config_data():
    for document in yaml.safe_load_all(MANIFEST.read_text()):
        if (
            isinstance(document, dict)
            and document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name") == "litellm-config"
        ):
            return document["data"]
    raise AssertionError("litellm-config ConfigMap not found")


def _exec_module(source, name):
    namespace = {"__name__": name}
    exec(compile(source, name, "exec"), namespace)
    return namespace


def test_tracker_emits_exact_backend_tokens_speed_and_ttft():
    tracker_module = _exec_module(
        _config_data()["active_request_tracking.py"],
        "active_request_tracking_contract",
    )

    class Clock:
        value = 100.0

        def __call__(self):
            return self.value

    clock = Clock()
    with tempfile.TemporaryDirectory() as temp_dir:
        active_file = Path(temp_dir) / "active.json"
        tracker = tracker_module["ActiveRequestTracker"](
            str(active_file),
            clock=clock,
        )
        tracker.start(
            "request-1",
            trace_id="trace-123",
            key_alias="openclaw-qwen36-prod",
            model="tooling",
            call_type="acompletion",
            api_base=None,
        )
        tracker.update_backend(
            "request-1",
            model="tooling",
            api_base=(
                "http://vllm-ornith-35b-nvfp4-mtp-dgx1."
                "llm.svc.cluster.local:8000/v1"
            ),
        )
        clock.value += 1
        tracker.update_usage(
            "request-1",
            prompt_tokens=3490,
            completion_tokens=1,
        )
        clock.value += 1
        tracker.update_usage(
            "request-1",
            prompt_tokens=3490,
            completion_tokens=34,
            force_flush=True,
        )

        request = json.loads(active_file.read_text())["request-1"]
        assert request["trace_id"] == "trace-123"
        assert request["server_id"] == "vllm-ornith-35b-nvfp4-mtp-dgx1"
        assert request["prompt_tokens"] == 3490
        assert request["completion_tokens"] == 34
        assert request["output_speed_tps"] == 33.0
        assert request["ttft_ms"] == 1000


def test_tracker_reads_usage_preserved_before_litellm_strips_the_chunk():
    tracker_module = _exec_module(
        _config_data()["active_request_tracking.py"],
        "active_request_tracking_hidden_usage_contract",
    )

    assert tracker_module["usage_from_response"](
        {
            "usage": None,
            "_hidden_params": {
                "live_usage": {
                    "prompt_tokens": 3490,
                    "completion_tokens": 34,
                }
            },
        }
    ) == (3490, 34)


def test_sidecar_preserves_exact_metrics(monkeypatch):
    active_api = _exec_module(
        _config_data()["active_requests_api.py"],
        "active_requests_api_contract",
    )
    monkeypatch.setattr(active_api["time"], "time", lambda: 102.0)

    normalized = active_api["_normalize"](
        {
            "request-1": {
                "trace_id": "trace-123",
                "alias": "openclaw-qwen36-prod",
                "model": "tooling",
                "call_type": "acompletion",
                "server_id": "vllm-ornith-35b-nvfp4-mtp-dgx1",
                "prompt_tokens": 3490,
                "completion_tokens": 34,
                "output_speed_tps": 33.0,
                "ttft_ms": 1000,
                "ts": 100.0,
            }
        }
    )

    assert normalized[0]["trace_id"] == "trace-123"
    assert normalized[0]["server_id"] == "vllm-ornith-35b-nvfp4-mtp-dgx1"
    assert normalized[0]["prompt_tokens"] == 3490
    assert normalized[0]["completion_tokens"] == 34
    assert normalized[0]["output_speed_tps"] == 33.0
    assert normalized[0]["ttft_ms"] == 1000


def test_hook_and_deployment_load_exact_metrics_tracker():
    text = MANIFEST.read_text()
    hook = _config_data()["litellm_strip_params.py"]

    assert "from active_request_tracking import (" in hook
    assert (
        "from litellm.litellm_core_utils.streaming_handler "
        "import CustomStreamWrapper"
    ) in hook
    assert "enable_continuous_usage(data, via_extra_body=" in hook
    assert "_preserve_live_usage_chunks()" in hook
    assert "_update_tracking_from_payload(kwargs, response_obj)" in hook
    assert "def _litellm_trace_id(data):" in hook
    assert "trace_id=_litellm_trace_id(data)" in hook
    assert '_track_end_from_payload(kwargs, "log_stream")' not in hook
    assert (
        "mountPath: /app/active_request_tracking.py, "
        "subPath: active_request_tracking.py"
    ) in text
    assert (
        'active-requests-api/revision: "20260726-request-history-trace"'
        in text
    )


def test_embedded_python_files_are_syntactically_valid():
    config = _config_data()
    for name in (
        "active_request_tracking.py",
        "active_requests_api.py",
        "litellm_strip_params.py",
    ):
        ast.parse(config[name], filename=name)


def test_resident_service_maps_to_its_dashboard_card():
    # El residente llm-tp se sirve desde `qwen38-flash-next.llm.svc`, que no
    # empieza por `vllm-`: sin el mapa, server_id salia null y el panel no podia
    # poner sus peticiones en su tarjeta ni cruzarlas con la cola del motor.
    tracker_module = _exec_module(
        _config_data()["active_request_tracking.py"],
        "active_request_tracking_resident_contract",
    )
    resolve = tracker_module["resolve_server_id"]
    base = "http://qwen38-flash-next.llm.svc.cluster.local:8000/v1"
    assert resolve(None, base) == "qwen38-flash-next-head"
    assert resolve("openai/qwen38-flash-next", base) == "qwen38-flash-next-head"
    # Lo externo sigue sin servidor: Alibaba no es una tarjeta del panel.
    assert resolve(None, "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1") is None


def test_continuous_usage_rides_extra_body_for_messages():
    # /v1/messages: el adaptador de LiteLLM reescribe `stream_options` a
    # {"include_usage": True} y tira continuous_usage_stats; extra_body pasa.
    tracker_module = _exec_module(
        _config_data()["active_request_tracking.py"],
        "active_request_tracking_extra_body_contract",
    )
    enable = tracker_module["enable_continuous_usage"]

    chat = enable({"stream": True})
    assert chat["stream_options"] == {"include_usage": True, "continuous_usage_stats": True}
    assert "extra_body" not in chat

    messages = enable({"stream": True, "extra_body": {"cache_salt": "refusal:1.0"}}, via_extra_body=True)
    assert messages["extra_body"]["stream_options"]["continuous_usage_stats"] is True
    # No pisa lo que ya traia el extra_body (el sello del alias abliterado).
    assert messages["extra_body"]["cache_salt"] == "refusal:1.0"

    assert enable({"stream": False}, via_extra_body=True) == {"stream": False}


def test_hook_feeds_tracker_from_the_openai_chunk_and_restores_the_seal():
    hook = _config_data()["litellm_strip_params.py"]
    # El parche de chunk_creator alimenta el tracker: en /v1/messages el hook del
    # proxy itera bytes SSE sin usage.
    assert "_track_live_chunk(self, usage)" in hook
    assert "tracking_id, model=None, api_base=api_base)" in hook
    # El residente por nombre directo pide el uso continuo SIN la admision de
    # compute-mode (que le quitaria el fallback a Alibaba).
    assert "and _served_by_local_vllm(model)" in hook
    # Tras meter extra_body en la peticion, el sello cache_salt de la deployment
    # se restaura: extra_body de la peticion sustituye al de la deployment.
    block = hook[hook.index("_stream_usage_extra = call_type"):hook.index("tracking_id = str(uuid.uuid4())")]
    assert block.count("enable_continuous_usage(data, via_extra_body=_stream_usage_extra)") == 2
    assert block.count("_preserve_uncensored_seal(data)") == 2
    assert block.index("enable_continuous_usage(data, via_extra_body") < block.index("_preserve_uncensored_seal(data)")


def test_tracking_id_also_rides_litellm_metadata_for_messages():
    hook = _config_data()["litellm_strip_params.py"]
    assert 'data["litellm_metadata"]["_tracking_id"] = tracking_id' in hook
