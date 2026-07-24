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
    assert "enable_continuous_usage(data)" in hook
    assert "_preserve_live_usage_chunks()" in hook
    assert "_update_tracking_from_payload(kwargs, response_obj)" in hook
    assert '_track_end_from_payload(kwargs, "log_stream")' not in hook
    assert (
        "mountPath: /app/active_request_tracking.py, "
        "subPath: active_request_tracking.py"
    ) in text
    assert (
        'active-requests-api/revision: "20260725-exact-stream-usage"'
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
