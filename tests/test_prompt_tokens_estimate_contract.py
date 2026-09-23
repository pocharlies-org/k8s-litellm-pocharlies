"""Tamaño ESTIMADO del prompt de una peticion en vuelo, antes del primer token.

vLLM manda el uso real con el primer token, o sea al acabar el prefill: en
/inferencia una peticion en «cola/prefill» salia sin tamaño justo cuando mas se
pregunta cuanto lleva. El hook mide los caracteres del body al entrar y el
tracker publica `prompt_tokens_estimate`. Aqui se fija:

  * se cuenta el texto de system/messages/tools, no el base64 de un adjunto;
  * la proporcion caracteres/token se APRENDE del prompt_tokens real, y una
    peticion con adjuntos no la ensucia;
  * `prompt_tokens` sigue siendo solo el del backend;
  * el estimado sobrevive al salto de fallback (el prompt es el mismo);
  * /internal/active-requests lo publica.
"""
import tempfile
import time
from pathlib import Path

from test_active_request_metrics_contract import _config_data, _exec_module
from test_active_requests_pod_scoping_contract import _module


def _tracking():
    return _exec_module(
        _config_data()["active_request_tracking.py"], "prompt_estimate_contract"
    )


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


def _start(tracker, rid, chars, media=False):
    return tracker.start(
        rid, trace_id="t", key_alias="claude-local", model="qwen38-flash-next",
        call_type="anthropic_messages", api_base=None,
        prompt_chars=chars, prompt_has_media=media,
    )


def test_cuenta_el_texto_de_messages_system_y_tools():
    prompt_chars = _tracking()["prompt_chars"]
    anthropic = {
        "model": "qwen38-flash-next",
        "max_tokens": 32000,
        "system": [{"type": "text", "text": "S" * 1000, "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": "U" * 2000},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "C" * 300}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "R" * 5000},
            ]},
        ],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    }
    chars, media = prompt_chars(anthropic)
    assert media is False
    # El texto manda; las claves suman poco y no se cuentan campos fuera del prompt.
    assert 8300 <= chars <= 8600, chars


def test_el_base64_de_una_imagen_no_cuenta_y_marca_adjunto():
    prompt_chars = _tracking()["prompt_chars"]
    b64 = "data:image/png;base64," + "A" * 400_000
    openai = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "T" * 100},
        {"type": "image_url", "image_url": {"url": b64}},
    ]}]}
    chars, media = prompt_chars(openai)
    assert media is True
    assert chars < 200


def test_estimado_al_entrar_y_proporcion_aprendida_del_real():
    tracking = _tracking()
    with tempfile.TemporaryDirectory() as temp_dir:
        tracker = tracking["ActiveRequestTracker"](
            str(Path(temp_dir) / "active.json"), clock=Clock()
        )
        row = _start(tracker, "r1", 350_000)
        assert row["prompt_tokens_estimate"] == 100_000
        assert row["prompt_tokens"] is None

        # El backend dice 125k: 2.8 caracteres/token. La proporcion se mueve.
        tracker.update_usage("r1", prompt_tokens=125_000, completion_tokens=0)
        assert tracker.snapshot()["r1"]["prompt_tokens"] == 125_000
        assert tracker.chars_per_token < 3.5
        learned = tracker.chars_per_token

        # Chunks posteriores con el mismo prompt no vuelven a calibrar.
        tracker.update_usage("r1", prompt_tokens=125_000, completion_tokens=5)
        assert tracker.chars_per_token == learned

        row = _start(tracker, "r2", 350_000)
        assert row["prompt_tokens_estimate"] == round(350_000 / learned)


def test_una_peticion_con_adjuntos_no_ensucia_la_proporcion():
    tracking = _tracking()
    with tempfile.TemporaryDirectory() as temp_dir:
        tracker = tracking["ActiveRequestTracker"](
            str(Path(temp_dir) / "active.json"), clock=Clock()
        )
        _start(tracker, "img", 1_000, media=True)
        tracker.update_usage("img", prompt_tokens=3_000, completion_tokens=0)
        assert tracker.chars_per_token == 3.5


def test_el_fallback_conserva_el_estimado():
    tracking = _tracking()
    with tempfile.TemporaryDirectory() as temp_dir:
        tracker = tracking["ActiveRequestTracker"](
            str(Path(temp_dir) / "active.json"), clock=Clock()
        )
        _start(tracker, "r1", 35_000)
        row = tracker.mark_fallback(
            "r1", from_model="qwen38-flash-next", to_model="alibaba-q38-flash"
        )
        assert row["prompt_tokens_estimate"] == 10_000
        assert row["prompt_tokens"] is None


def test_active_requests_publica_el_estimado():
    sidecar = _module("active_requests_api.py")
    items = sidecar["_normalize"]({
        "r1": {
            "request_id": "r1", "alias": "claude-local", "model": "qwen38-flash-next",
            "ts": time.time(), "prompt_tokens": None, "prompt_tokens_estimate": 81_234,
        }
    })
    assert items[0]["prompt_tokens_estimate"] == 81_234
    assert items[0]["prompt_tokens"] is None
