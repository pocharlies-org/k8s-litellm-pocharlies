"""Contrato INFRA-269 (C1-fila): el sello del motivo del fallback del Router.

Regla 1 del architect: el callback de LiteLLM sella DENTRO de
`spend_logs_metadata` de la fila de "LiteLLM_SpendLogs" los CUATRO campos
juntos — `fallback_motivo`, `fallback_ts` (epoch s), `fallback_desde`,
`fallback_hacia` — y la fila queda auto-descriptiva (el lector no necesita el
tracker). Regla 2: el enum de `motivo` es CERRADO
{sobrecarga, error, servicio-caido, sin-clasificar}; `reroute` NO es motivo
(es route_kind y aqui no llega: solo hay saltos reales del Router).

Mecanismo de dos tiempos: (i) el hook `async_post_call_failure_deployment_hook`
(fallo POR INTENTO, oficial v1.105.1) clasifica la excepcion y guarda el motivo
en el tracker por `_tracking_id`; (ii) `log_pre_api_call` del salto siguiente
lo lee y sella (sin entrada del tracker: `sin-clasificar`).

El codigo probado VIVE en el ConfigMap; se extrae como en
test_refusal_probe_stamp_contract.py (hook) y
test_active_request_metrics_contract.py (tracker).
"""
import ast
import time
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
WANT_FN = {
    "_metadata_candidates",
    "_tracking_id_candidates",
    "_tracking_id_from_payload",
    "_fallback_hop_from_payload",
    "_fallback_motivo_from_exception",
    "_stamp_fallback_hop",
}
WANT_ASSIGN = {"FALLBACK_MOTIVOS"}
ENUM = ("sobrecarga", "error", "servicio-caido", "sin-clasificar")
CAMPOS = ("fallback_motivo", "fallback_ts", "fallback_desde", "fallback_hacia")


def _config_data():
    for document in yaml.safe_load_all(MANIFEST.read_text()):
        if (
            isinstance(document, dict)
            and document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name") == "litellm-config"
        ):
            return document["data"]
    raise AssertionError("litellm-config ConfigMap not found")


def _hook_source():
    return _config_data()["litellm_strip_params.py"]


@pytest.fixture(scope="module")
def hook():
    tree = ast.parse(_hook_source())
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (
            isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id in WANT_ASSIGN for t in n.targets
            )
        )
    ]
    missing = (WANT_FN | WANT_ASSIGN) - {
        n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in keep
    }
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("hookfallback")
    import logging

    mod.__dict__["log"] = logging.getLogger("hooktest")
    mod.__dict__["time"] = time
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


@pytest.fixture(scope="module")
def tracker_module():
    namespace = {"__name__": "active_request_tracking_contract"}
    exec(compile(_config_data()["active_request_tracking.py"], "art", "exec"), namespace)  # noqa: S102
    return namespace


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


def _tracker(tracker_module, tmp_path):
    clock = Clock()
    return clock, tracker_module["ActiveRequestTracker"](
        str(tmp_path / "active.json"), clock=clock
    )


def _exc(name, status=None, message="boom"):
    cls = type(name, (Exception,), {})
    e = cls(message)
    if status is not None:
        e.status_code = status
    return e


# ── el enum es literal y cerrado (regla 2) ────────────────────────────────────
def test_enum_literal(hook):
    assert tuple(hook.FALLBACK_MOTIVOS) == ENUM


# ── clasificacion: el mapeo de la regla 2, tal cual ───────────────────────────
@pytest.mark.parametrize(
    "exc, esperado",
    [
        # sobrecarga: 429/RateLimit (y el tope de en vuelo, que LiteLLM
        # levanta como RateLimitError 429 — k8s-litellm #224).
        (_exc("RateLimitError", 429), "sobrecarga"),
        (_exc("SomeUpstreamError", 429), "sobrecarga"),
        (_exc("UpstreamError", 200, "Upstream rate limit exceeded"), "sobrecarga"),
        (_exc("UpstreamError", 200, "Too Many Requests"), "sobrecarga"),
        # error: Timeout, APIError y 5xx con respuesta.
        (_exc("Timeout", 408), "error"),
        (_exc("APITimeoutError", 408), "error"),
        (_exc("APIError", 500), "error"),
        (_exc("ServiceUnavailableError", 503), "error"),
        (_exc("InternalServerError", 500), "error"),
        # servicio-caido: conexion rechazada (litellm la envuelve en
        # APIConnectionError, que trae status 500: la precedencia importa) y el
        # grupo sin despliegues vivos.
        (_exc("APIConnectionError", 500, "Connection error."), "servicio-caido"),
        (_exc("ConnectionRefusedError", message="Connection refused"), "servicio-caido"),
        (_exc("ConnectError", message="connection refused"), "servicio-caido"),
        (
            _exc(
                "BadRequestError",
                400,
                "You passed in model=tooling. There are no healthy deployments for this model",
            ),
            "servicio-caido",
        ),
        # sin-clasificar: catch-all (400/401/404, nada de lo anterior).
        (_exc("BadRequestError", 400, "invalid request"), "sin-clasificar"),
        (_exc("AuthenticationError", 401), "sin-clasificar"),
        (_exc("NotFoundError", 404), "sin-clasificar"),
    ],
)
def test_clasificacion(hook, exc, esperado):
    assert hook._fallback_motivo_from_exception(exc) == esperado


def test_sin_excepcion_es_sin_clasificar(hook):
    assert hook._fallback_motivo_from_exception(None) == "sin-clasificar"


def test_excepcion_rara_no_rompe(hook):
    """El catch-all es `sin-clasificar`, nunca una excepcion (regla 1)."""

    class Rara(Exception):
        def __str__(self):
            raise RuntimeError("str estropeada")

    assert hook._fallback_motivo_from_exception(Rara()) == "sin-clasificar"


def test_status_no_numerico_no_rompe(hook):
    e = _exc("RateLimitError")
    e.status_code = "no-pepe"
    assert hook._fallback_motivo_from_exception(e) == "sobrecarga"  # por el nombre


def test_toda_clasificacion_cae_en_el_enum(hook):
    for exc in (None, _exc("X"), _exc("RateLimitError", 429), _exc("Timeout", 408)):
        assert hook._fallback_motivo_from_exception(exc) in ENUM


# ── el sello: los cuatro campos DENTRO de spend_logs_metadata ────────────────
def test_sello_anidado_los_cuatro_juntos(hook):
    kwargs = {
        "litellm_params": {
            "metadata": {"spend_logs_metadata": {"refusal_lambda": "1.0"}}
        }
    }
    assert hook._stamp_fallback_hop(kwargs, ("qwen38-flash-next", "alibaba-q38-flash"), "servicio-caido") is True
    slm = kwargs["litellm_params"]["metadata"]["spend_logs_metadata"]
    assert slm["fallback_motivo"] == "servicio-caido"
    assert slm["fallback_desde"] == "qwen38-flash-next"
    assert slm["fallback_hacia"] == "alibaba-q38-flash"
    assert isinstance(slm["fallback_ts"], int)
    assert abs(slm["fallback_ts"] - time.time()) < 5
    assert slm["refusal_lambda"] == "1.0"  # no pisa lo demas
    # nunca clave plana: fuera de spend_logs_metadata no hay nada de esto
    assert not (set(CAMPOS) & set(kwargs["litellm_params"]["metadata"]))


def test_sello_respeta_la_prioridad_del_lector(hook):
    """get_litellm_metadata_from_kwargs (v1.105.1) lee litellm_metadata si
    existe y no esta vacio; si no, metadata. El sello va al mismo bucket."""
    kwargs = {
        "litellm_params": {
            "litellm_metadata": {"spend_logs_metadata": {}},
            "metadata": {"spend_logs_metadata": {}},
        }
    }
    assert hook._stamp_fallback_hop(kwargs, ("a", "b"), "error") is True
    assert "fallback_motivo" in kwargs["litellm_params"]["litellm_metadata"]["spend_logs_metadata"]
    assert "fallback_motivo" not in kwargs["litellm_params"]["metadata"]["spend_logs_metadata"]


def test_sello_litellm_metadata_vacio_usa_metadata(hook):
    kwargs = {"litellm_params": {"litellm_metadata": {}, "metadata": {"model_group": "x"}}}
    assert hook._stamp_fallback_hop(kwargs, ("a", "b"), "error") is True
    assert "fallback_motivo" in kwargs["litellm_params"]["metadata"]["spend_logs_metadata"]


def test_sello_sin_bucket_crea_metadata(hook):
    kwargs = {"litellm_params": {"api_base": "http://x"}}
    assert hook._stamp_fallback_hop(kwargs, ("a", "b"), "sobrecarga") is True
    assert kwargs["litellm_params"]["metadata"]["spend_logs_metadata"]["fallback_motivo"] == "sobrecarga"


def test_sello_sin_hop_no_toca_nada(hook):
    kwargs = {"litellm_params": {"metadata": {"spend_logs_metadata": {"k": "v"}}}}
    assert hook._stamp_fallback_hop(kwargs, None, "error") is False
    assert kwargs == {"litellm_params": {"metadata": {"spend_logs_metadata": {"k": "v"}}}}


def test_sello_defensivo_nunca_levanta(hook):
    """Un fallo del sello jamas rompe la peticion (patrón _stamp_claude_class):
    el call-site lo envuelve en try/except, y con entradas raras devuelve False
    en vez de subir."""
    assert hook._stamp_fallback_hop(None, ("a", "b"), "error") is False
    assert hook._stamp_fallback_hop({"litellm_params": "no-dict"}, ("a", "b"), "error") is False
    assert hook._stamp_fallback_hop({"litellm_params": {"metadata": "no-dict"}}, ("a", "b"), "error") is True
    # metadata no-dict: se reemplaza por el bucket nuevo (el lector solo
    # acepta dicts; escribir uno es lo que hace la fila legible)


# ── el tracker: guardar/leer el motivo por _tracking_id ──────────────────────
def test_tracker_guarda_y_lee(tmp_path, tracker_module):
    clock, tracker = _tracker(tracker_module, tmp_path)
    assert tracker.fallback_motivo("req-1") is None
    tracker.note_fallback_motivo("req-1", "servicio-caido")
    assert tracker.fallback_motivo("req-1") == "servicio-caido"


def test_tracker_sobrevive_al_cierre_de_la_fila(tmp_path, tracker_module):
    """El fallo del primario cierra la fila (end()) antes o despues del salto
    (carrera); el motivo tiene que seguir leyendose para el sello del hop."""
    clock, tracker = _tracker(tracker_module, tmp_path)
    tracker.start("req-1", key_alias="k", model="m", call_type="acompletion", api_base=None)
    tracker.note_fallback_motivo("req-1", "sobrecarga")
    tracker.end("req-1")
    assert tracker.fallback_motivo("req-1") == "sobrecarga"


def test_tracker_caduca_tras_la_ventana(tmp_path, tracker_module):
    clock, tracker = _tracker(tracker_module, tmp_path)
    tracker.note_fallback_motivo("req-1", "error")
    clock.value += tracker.revive_window_s + 1
    assert tracker.fallback_motivo("req-1") is None


def test_tracker_ignora_entradas_vacias(tmp_path, tracker_module):
    clock, tracker = _tracker(tracker_module, tmp_path)
    tracker.note_fallback_motivo("", "error")
    tracker.note_fallback_motivo("req-1", "")
    assert tracker.fallback_motivo("req-1") is None


def test_snapshot_poda_motivos_viejos(tmp_path, tracker_module):
    clock, tracker = _tracker(tracker_module, tmp_path)
    tracker.note_fallback_motivo("req-viejo", "error")
    clock.value += tracker.revive_window_s + 1
    tracker.snapshot()
    assert tracker._fallback_motivos == {}


# ── cableado: el hook de fallo y el sello estan donde deben ──────────────────
def test_hook_de_fallo_por_intento_guarda_en_el_tracker():
    src = _hook_source()
    assert "async def async_post_call_failure_deployment_hook" in src
    assert "request_tracker.note_fallback_motivo(" in src
    assert "_fallback_motivo_from_exception(exception)" in src


def test_log_pre_api_call_sella_el_salto():
    src = _hook_source()
    assert '_stamp_fallback_hop(kwargs, hop, motivo or "sin-clasificar")' in src
    # y la deteccion del hop es la del Router, no una copia
    assert "hop = _fallback_hop_from_payload(kwargs)" in src


def test_contrato_de_las_cuatro_claves_en_el_codigo():
    src = _hook_source()
    for campo in CAMPOS:
        assert f'_slm["{campo}"]' in src, campo


# ── el grupo de prueba del manifiesto (regla 6: sin tocar el residente) ──────
def test_grupo_de_prueba_en_config():
    import yaml as _yaml

    docs = [d for d in _yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    config = _yaml.safe_load(cm["data"]["config.yaml"])
    groups = {}
    for entry in config["model_list"]:
        groups.setdefault(entry["model_name"], []).append(entry)
    probe = groups.get("infra1081-fallback-probe")
    mock = groups.get("infra1081-fallback-mock")
    assert probe and mock, "el grupo de prueba ya no esta en el model_list"
    # primario: puerto que rechaza conexion, sin reintento ni cooldown
    lp = probe[0]["litellm_params"]
    assert "127.0.0.1:9" in lp["api_base"]
    assert lp.get("num_retries") == 0
    assert lp.get("cooldown_time") == 0
    # destino: mock sin tokens y coste 0 declarado
    assert mock[0]["litellm_params"].get("mock_response")
    assert mock[0]["model_info"]["input_cost_per_token"] == 0
    assert mock[0]["model_info"]["output_cost_per_token"] == 0
    # el grafo global NO se toca (lista blanca del owner): la ráfaga declara el
    # salto por petición con el parametro `fallbacks` del body
    edges = config["router_settings"]["fallbacks"]
    graph = {s: d for e in edges for s, d in e.items()}
    assert "infra1081-fallback-probe" not in graph
    assert graph == {
        "qwen38-flash-next": ["alibaba-q38-flash"],
        "correo-clasificador": ["alibaba-q38-flash"],
    }
