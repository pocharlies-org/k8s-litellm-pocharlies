"""Contrato de la FASE B del detector de refusal: el sello de la SONDA del vLLM.

El panel `/api/llm/refusal-rates` (dgx-infra) lee de `spend_logs_metadata`:

  * `refusal_score` — TEXTO numerico. El SQL lo pasa por la regex
    `^-?[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?$` antes del cast: un 'nan' o un
    'None' se ignoraria en silencio, y un float suelto no es texto.
  * `refusal_flag` — booleano JSON (el SQL compara `= 'true'`). SOLO con
    calibracion: un flag sin `refusal_cal` es un veredicto inventado.
  * `refusal_cal` — id de la calibracion.

La sonda llega por dos formas (ver refusal_probe.py en k8s-ai-pocharlies):
streaming -> `provider_specific_fields.refusal_probe` del chunk (LiteLLM tambien
la deja como atributo), no-stream -> choice.provider_specific_fields.

El codigo probado VIVE en el ConfigMap; se extrae como en
test_refusal_text_stamp_contract.py.
"""
import ast
import re
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
WANT_FN = {
    "_extract_refusal_probe",
    "_refusal_probe_fields",
    "_slm_update",
    "_stamp_refusal_probe",
}
PANEL_SCORE_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$")


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in (x for x in yaml.safe_load_all(MANIFEST.read_text()) if x)
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def hook():
    tree = ast.parse(_hook_source())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANT_FN]
    missing = WANT_FN - {n.name for n in keep}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("hookprobe")
    import logging
    mod.__dict__["log"] = logging.getLogger("hooktest")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


RP_CAL = {"v": 1, "score": 0.183271, "feature": "prompt_last_mean",
          "flag": True, "cal": "cal-20260924a"}
RP_NOCAL = {"v": 1, "score": -0.0213, "feature": "prompt_last_mean",
            "flag": None, "cal": None}


# ── extraccion: las tres formas en que llega ─────────────────────────────────
def test_extract_stream_top_level_psf(hook):
    chunk = types.SimpleNamespace(provider_specific_fields={"refusal_probe": RP_CAL},
                                  choices=[])
    assert hook._extract_refusal_probe(chunk) == RP_CAL


def test_extract_stream_attribute(hook):
    chunk = types.SimpleNamespace(refusal_probe=RP_CAL, choices=[])
    assert hook._extract_refusal_probe(chunk) == RP_CAL


def test_extract_nonstream_choice_psf(hook):
    choice = types.SimpleNamespace(provider_specific_fields={"refusal_probe": RP_NOCAL})
    resp = types.SimpleNamespace(choices=[choice])
    assert hook._extract_refusal_probe(resp) == RP_NOCAL


def test_extract_dict_forms(hook):
    assert hook._extract_refusal_probe(
        {"choices": [{"refusal_probe": RP_CAL}]}) == RP_CAL
    assert hook._extract_refusal_probe(
        {"provider_specific_fields": {"refusal_probe": RP_CAL}, "choices": []}) == RP_CAL


@pytest.mark.parametrize("obj", [
    None, b"event: x", {"choices": []},
    types.SimpleNamespace(choices=[types.SimpleNamespace(provider_specific_fields=None)]),
    {"provider_specific_fields": {"otra_cosa": 1}},
])
def test_extract_absent_is_none(hook, obj):
    """Alibaba/OpenAI/anthropic no traen sonda: None, nunca una excepcion."""
    assert hook._extract_refusal_probe(obj) is None


# ── campos: la forma que el panel sabe leer ─────────────────────────────────
def test_fields_with_calibration(hook):
    f = hook._refusal_probe_fields(RP_CAL)
    assert PANEL_SCORE_RE.match(f["refusal_score"])
    assert float(f["refusal_score"]) == pytest.approx(0.183271, abs=1e-6)
    assert f["refusal_flag"] is True          # booleano JSON, no 'True' ni 1
    assert f["refusal_cal"] == "cal-20260924a"


def test_fields_without_calibration_has_no_flag(hook):
    f = hook._refusal_probe_fields(RP_NOCAL)
    assert PANEL_SCORE_RE.match(f["refusal_score"])
    assert "refusal_flag" not in f and "refusal_cal" not in f


def test_flag_without_cal_is_dropped(hook):
    f = hook._refusal_probe_fields({"score": 0.5, "flag": True, "cal": None})
    assert "refusal_flag" not in f


@pytest.mark.parametrize("score", [None, "0.3", float("nan"), float("inf"), True])
def test_bad_score_seals_nothing(hook, score):
    assert hook._refusal_probe_fields({"score": score, "flag": None}) is None


def test_tiny_score_still_matches_panel_regex(hook):
    f = hook._refusal_probe_fields({"score": 1e-7})
    assert PANEL_SCORE_RE.match(f["refusal_score"])


# ── el sello: anidado, dos vias, no pisa lo demas ───────────────────────────
def test_stamp_nested_and_keeps_other_keys(hook):
    data = {"metadata": {"spend_logs_metadata": {"refusal_lambda": "0.0",
                                                 "refusal_text": "answer"}}}
    chunk = types.SimpleNamespace(provider_specific_fields={"refusal_probe": RP_CAL},
                                  choices=[])
    assert hook._stamp_refusal_probe(data, chunk) is True
    slm = data["metadata"]["spend_logs_metadata"]
    assert slm["refusal_lambda"] == "0.0" and slm["refusal_text"] == "answer"
    assert slm["refusal_flag"] is True and slm["refusal_cal"] == "cal-20260924a"
    assert "refusal_score" not in data["metadata"]   # nunca clave plana


def test_stamp_reaches_live_logging_obj(hook):
    """En streaming request_data es una COPIA: la fila la lee el logging_obj."""
    lp_meta = {}
    log_obj = types.SimpleNamespace(model_call_details={"litellm_params": {"metadata": lp_meta}})
    data = {"litellm_metadata": {}, "litellm_logging_obj": log_obj}
    assert hook._stamp_refusal_probe(data, {"choices": [{"refusal_probe": RP_NOCAL}]})
    assert "refusal_score" in data["litellm_metadata"]["spend_logs_metadata"]
    assert "refusal_score" in lp_meta["spend_logs_metadata"]


def test_stamp_without_probe_touches_nothing(hook):
    data = {"metadata": {"spend_logs_metadata": {"refusal_text": "answer"}}}
    assert hook._stamp_refusal_probe(data, {"choices": [{"message": {"content": "x"}}]}) is False
    assert data == {"metadata": {"spend_logs_metadata": {"refusal_text": "answer"}}}


# ── cableado: las dos llamadas existen en los hooks ─────────────────────────
def test_hooks_call_the_stamp():
    src = _hook_source()
    assert "_stamp_refusal_probe(data, response)" in src           # no-stream
    assert "_stamp_refusal_probe(request_data, chunk)" in src      # stream
