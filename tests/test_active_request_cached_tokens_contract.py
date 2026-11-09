"""DGX-951: cada fila en vuelo lleva cuántos de sus tokens de entrada salieron de la caché de prefijo.

El vLLM los da en `usage.prompt_tokens_details.cached_tokens` (en cada chunk desde el parche de su receta, y
antes solo en el último); el tracker los guarda en `cached_tokens` y el sidecar los sirve tal cual al panel.
Sin cifra en el uso, la fila sigue con `cached_tokens: None`: el panel no inventa nada.
"""
import json
import tempfile
from pathlib import Path

from test_active_request_metrics_contract import _config_data, _exec_module


def _tracker_module():
    return _exec_module(_config_data()["active_request_tracking.py"], "active_request_tracking_cached")


def test_cached_from_response_lee_el_uso_de_cada_proveedor():
    cached = _tracker_module()["cached_from_response"]
    detalles = {"prompt_tokens": 50_143, "prompt_tokens_details": {"cached_tokens": 45_120}}
    assert cached({"usage": detalles}) == 45_120
    # el chunk saneado por LiteLLM: el uso viaja en _hidden_params.live_usage
    assert cached({"usage": None, "_hidden_params": {"live_usage": detalles}}) == 45_120
    assert cached({"usage": {"prompt_tokens": 10, "cache_read_input_tokens": 7}}) == 7
    assert cached({"usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}}) == 0
    assert cached({"usage": {"prompt_tokens": 10, "prompt_tokens_details": None}}) is None
    assert cached({"usage": {"prompt_tokens": 10}}) is None
    assert cached({}) is None


def test_el_tracker_guarda_los_cached_tokens_y_un_fallback_los_borra():
    mod = _tracker_module()
    with tempfile.TemporaryDirectory() as temp_dir:
        active_file = Path(temp_dir) / "active.json"
        tracker = mod["ActiveRequestTracker"](str(active_file), clock=lambda: 100.0)
        tracker.start("r1", trace_id=None, key_alias="claude-x86", model="qwen38-flash-next",
                      call_type="acompletion", api_base=None)
        assert json.loads(active_file.read_text())["r1"]["cached_tokens"] is None
        tracker.update_usage("r1", prompt_tokens=50_143, completion_tokens=0)
        assert json.loads(active_file.read_text())["r1"]["cached_tokens"] is None
        tracker.update_usage("r1", prompt_tokens=50_143, completion_tokens=1, cached_tokens=45_120,
                             force_flush=True)
        assert json.loads(active_file.read_text())["r1"]["cached_tokens"] == 45_120
        tracker.mark_fallback("r1", from_model="qwen38-flash-next", to_model="alibaba-q38-flash",
                              api_base=None, reason="router_fallback")
        assert json.loads(active_file.read_text())["r1"]["cached_tokens"] is None


def test_el_sidecar_sirve_los_cached_tokens(monkeypatch):
    api = _exec_module(_config_data()["active_requests_api.py"], "active_requests_api_cached")
    monkeypatch.setattr(api["time"], "time", lambda: 102.0)
    fila = {"alias": "claude-x86", "model": "qwen38-flash-next", "server_id": "qwen38-flash-next",
            "prompt_tokens": 50_143, "cached_tokens": 45_120, "completion_tokens": 756, "ts": 100.0}
    assert api["_normalize"]({"r1": fila})[0]["cached_tokens"] == 45_120
    assert api["_normalize"]({"r1": {**fila, "cached_tokens": None}})[0]["cached_tokens"] is None


def test_el_hook_pasa_los_cached_tokens_en_los_dos_caminos():
    hook = _config_data()["litellm_strip_params.py"]
    assert "    cached_from_response,\n)" in hook
    assert hook.count("cached_tokens=cached_from_response(") == 2
