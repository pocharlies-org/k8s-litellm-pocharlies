"""Contrato de la FASE A del detector de refusal: el sello `refusal_text`.

El panel `/api/llm/refusal-rates` (dgx-infra) tiene el denominador en
`metadata->'spend_logs_metadata'->>'refusal_text' IS NOT NULL` y el numerador en
`= 'refusal'`. Este test fija el contrato entre el hook de LiteLLM y ese panel:

  * el valor escrito es EXACTAMENTE 'refusal' o 'answer' (nunca True/1/'REFUSAL':
    el numerador se iría a cero en silencio y el panel pintaría 0 % con cara de
    dato — ver el trailer `n_text_unknown` en refusal_rates.py);
  * se escribe ANIDADO dentro de `spend_logs_metadata` (una clave plana fuera de
    ahí la tira la allowlist de `SpendLogsMetadata`);
  * sin texto de asistente NO se sella (embeddings/transcripción/fallos no deben
    inflar el denominador);
  * respeta las dos vias de metadata (`metadata` y `litellm_metadata`).

El código probado VIVE en el ConfigMap (es la copia que corre); se extrae igual
que en test_uncensored_no_fallback_contract.py.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {
    "_looks_like_refusal",
    "_extract_response_text",
    "_extract_delta_text",
    "_stamp_refusal_text",
}
WANT_CONST = {"_REFUSAL_OPENERS"}


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in (x for x in yaml.safe_load_all(MANIFEST.read_text()) if x)
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def hook():
    tree = ast.parse(_hook_source())
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("hookpure")
    ns = mod.__dict__
    ns["Any"] = object  # por si el hook reanota; no se usa en runtime
    import logging
    ns["log"] = logging.getLogger("hooktest")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), ns)  # noqa: S102
    return mod


# ── clasificación ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "I'm sorry, but I can't help with that.",
    "I cannot assist with that request.",
    "As an AI, I don't have personal opinions.",
    "Lo siento, pero no puedo ayudarte con eso.",
    "No puedo proporcionar ese tipo de información.",
])
def test_refusal_openers_detect(hook, text):
    assert hook._looks_like_refusal(text) is True


@pytest.mark.parametrize("text", [
    "Sure — here is how you sort a list in Python:\n\n```python\nsorted(xs)\n```",
    "The capital of France is Paris.",
    "Claro, te explico el algoritmo paso a paso.",
])
def test_answers_not_flagged(hook, text):
    assert hook._looks_like_refusal(text) is False


# ── extracción de texto de la respuesta ──────────────────────────────────────
def test_extract_response_text_dict(hook):
    resp = {"choices": [{"message": {"content": "hello there"}}]}
    assert hook._extract_response_text(resp) == "hello there"


def test_extract_response_text_object(hook):
    msg = types.SimpleNamespace(content="obj answer")
    resp = types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    assert hook._extract_response_text(resp) == "obj answer"


def test_extract_response_text_blocks(hook):
    resp = {"choices": [{"message": {"content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}]}
    assert hook._extract_response_text(resp) == "ab"


def test_extract_response_text_none_when_no_choices(hook):
    assert hook._extract_response_text({"choices": []}) is None
    assert hook._extract_response_text(None) is None


def test_extract_delta_text(hook):
    assert hook._extract_delta_text({"choices": [{"delta": {"content": "tok"}}]}) == "tok"
    assert hook._extract_delta_text({"choices": [{"delta": {}}]}) == ""


def test_extract_delta_text_uses_litellm_extractor_for_anthropic(hook, monkeypatch):
    """El chunk de /v1/messages no trae choices[].delta.content: litellm lo
    normaliza y su get_response_string es el que saca el texto. Sin usarlo, el
    sello anthropic salía vacío (0/80 medido). Se simula ese extractor."""
    calls = []

    class FakeLitellm:
        @staticmethod
        def get_response_string(response_obj=None):
            # un ModelResponseStream anthropic normalizado: el texto NO está en
            # choices[].delta.content, así que el fallback OpenAI daría ""
            calls.append(response_obj)
            return "hola"

    monkeypatch.setattr(hook, "litellm", FakeLitellm, raising=False)
    anthropic_chunk = {"type": "content_block_delta", "delta": {"text": "hola"}}
    assert hook._extract_delta_text(anthropic_chunk) == "hola"
    assert calls  # pasó por el extractor canónico, no por el fallback


# ── el sello: forma, anidamiento y condiciones ───────────────────────────────
def test_stamp_writes_refusal_nested(hook):
    data = {"metadata": {"spend_logs_metadata": {"refusal_lambda": "0.0"}}}
    hook._stamp_refusal_text(data, "I'm sorry, but I can't do that.")
    slm = data["metadata"]["spend_logs_metadata"]
    assert slm["refusal_text"] == "refusal"
    # no pisa el sello del dial que ya estaba
    assert slm["refusal_lambda"] == "0.0"


def test_stamp_writes_answer(hook):
    data = {}
    hook._stamp_refusal_text(data, "Here is your answer.")
    assert data["metadata"]["spend_logs_metadata"]["refusal_text"] == "answer"


def test_stamp_respects_litellm_metadata_variant(hook):
    data = {"litellm_metadata": {}}
    hook._stamp_refusal_text(data, "No puedo ayudarte con eso.")
    assert data["litellm_metadata"]["spend_logs_metadata"]["refusal_text"] == "refusal"
    assert "metadata" not in data  # no crea la otra via


def test_stamp_skips_when_no_text(hook):
    data = {"metadata": {}}
    hook._stamp_refusal_text(data, "")
    hook._stamp_refusal_text(data, None)
    assert "spend_logs_metadata" not in data["metadata"]


def test_stamp_value_is_always_in_the_contract_set(hook):
    # cualquier texto produce exactamente 'refusal' o 'answer'
    for t in ["", "x", "I cannot", "totally fine", "no puedo"]:
        data = {"metadata": {}}
        hook._stamp_refusal_text(data, t or "nonempty")
        assert data["metadata"]["spend_logs_metadata"]["refusal_text"] in ("refusal", "answer")


def test_stamp_never_raises_on_garbage(hook):
    # es telemetría: un request_data raro no puede romper la petición
    for bad in [None, 42, "string", {"metadata": "notadict"}]:
        hook._stamp_refusal_text(bad, "I cannot help.")  # no debe lanzar
