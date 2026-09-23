"""Origen del enrutado por fila del tracker: direct | reroute | fallback.

El panel VLLM SERVERS de dgx-infra pinta las peticiones de Alibaba en vivo y
marca las que ERAN del residente local. Para eso cada fila lleva route_kind,
route_from y route_reason. Aqui se fija lo que no es obvio:

  * un fallback del Router revive la fila que el fallo del primario ya cerro
    (mismo _tracking_id, el fallo llega antes que el salto);
  * el fallo tardio del primario NO cierra la fila que ya sirve el fallback;
  * un evento de logging tardio del fallback ya cerrado no la resucita.
"""
import tempfile
from pathlib import Path

from test_active_request_metrics_contract import _config_data, _exec_module


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


def _tracker(temp_dir, clock):
    module = _exec_module(
        _config_data()["active_request_tracking.py"], "route_origin_contract"
    )
    return module["ActiveRequestTracker"](
        str(Path(temp_dir) / "active.json"), clock=clock
    )


def _start(tracker, rid="r1", model="qwen38-flash-next", **route):
    tracker.start(
        rid, trace_id="t", key_alias="claude-local", model=model,
        call_type="anthropic_messages", api_base=None, **route,
    )


def test_fila_directa_por_defecto():
    with tempfile.TemporaryDirectory() as d:
        tracker = _tracker(d, Clock())
        _start(tracker, model="alibaba-q38-flash")
        row = tracker.snapshot()["r1"]
        assert row["route_kind"] == "direct"
        assert row["route_from"] is None and row["route_reason"] is None


def test_reroute_de_session_router_sabe_de_donde_viene():
    with tempfile.TemporaryDirectory() as d:
        tracker = _tracker(d, Clock())
        _start(tracker, model="alibaba-q38-flash", route_kind="reroute",
               route_from="qwen38-flash-next", route_reason="sticky_alibaba")
        row = tracker.snapshot()["r1"]
        assert (row["route_kind"], row["route_from"], row["route_reason"]) == (
            "reroute", "qwen38-flash-next", "sticky_alibaba")


def test_fallback_revive_la_fila_cerrada_por_el_fallo_del_primario():
    clock = Clock()
    with tempfile.TemporaryDirectory() as d:
        tracker = _tracker(d, clock)
        _start(tracker)
        tracker.update_usage("r1", prompt_tokens=80000, completion_tokens=0)
        clock.value = 340.0  # timeout 240 del residente
        tracker.end("r1", model="qwen38-flash-next")
        assert "r1" not in tracker.snapshot()
        tracker.mark_fallback("r1", from_model="qwen38-flash-next",
                              to_model="alibaba-q38-flash")
        row = tracker.snapshot()["r1"]
        assert row["model"] == "alibaba-q38-flash"
        assert (row["route_kind"], row["route_from"]) == ("fallback", "qwen38-flash-next")
        assert row["ts"] == 100.0, "el reloj del cliente sigue desde el principio"
        assert row["route_at"] == 340.0
        assert row["prompt_tokens"] is None, "metricas del intento, no del primario"


def test_fallo_tardio_del_primario_no_cierra_el_fallback():
    with tempfile.TemporaryDirectory() as d:
        tracker = _tracker(d, Clock())
        _start(tracker)
        tracker.mark_fallback("r1", from_model="qwen38-flash-next",
                              to_model="alibaba-q38-flash")
        tracker.end("r1", model="qwen38-flash-next")
        assert "r1" in tracker.snapshot()
        tracker.end("r1", model="alibaba-q38-flash")
        assert "r1" not in tracker.snapshot()


def test_logging_tardio_del_fallback_cerrado_no_resucita():
    with tempfile.TemporaryDirectory() as d:
        tracker = _tracker(d, Clock())
        _start(tracker)
        tracker.mark_fallback("r1", from_model="qwen38-flash-next",
                              to_model="alibaba-q38-flash")
        tracker.end("r1")
        tracker.mark_fallback("r1", from_model="qwen38-flash-next",
                              to_model="alibaba-q38-flash")
        assert "r1" not in tracker.snapshot()


def test_no_revive_fuera_de_la_ventana():
    clock = Clock()
    with tempfile.TemporaryDirectory() as d:
        tracker = _tracker(d, clock)
        _start(tracker)
        tracker.end("r1")
        clock.value += tracker.revive_window_s + 1
        tracker.mark_fallback("r1", from_model="qwen38-flash-next",
                              to_model="alibaba-q38-flash")
        assert "r1" not in tracker.snapshot()


def test_hook_detecta_el_salto_por_el_metadata_del_router():
    source = _config_data()["litellm_strip_params.py"]
    assert "original_model_group" in source
    assert "def log_pre_api_call(self, model, messages, kwargs)" in source
    assert "_note_fallback_from_payload(kwargs)" in source
    assert 'route_kind="reroute" if _rerouted else "direct"' in source
