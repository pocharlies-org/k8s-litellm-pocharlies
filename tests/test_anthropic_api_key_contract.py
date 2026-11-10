"""La API key de pago de Anthropic por litellm (INFRA-1223): alias `anthropic-*` tras un gate fail-closed.

`models: []` deja pedir cualquier modelo en 7 de las 10 keys vivas: sin el gate, cualquier key gastaria la key de
pago (tope de 150 USD/mes en la Console). Va por PREFIJO y nace cerrado, como grok-* y gpt-*.
"""
from pathlib import Path

import pytest
import yaml

from manifest_docs import hook_gate

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
WANT_FN = {"_anthropic_access_denied"}
WANT_CONST = {"ANTHROPIC_ALIAS_PREFIX", "ANTHROPIC_ALLOWED_KEY_ALIASES"}
ALLOWED_KEYS = ("voz-duplex", "nova")


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _gate(env):
    return hook_gate(WANT_FN, WANT_CONST, env, "anthropicgate")


@pytest.fixture(scope="module")
def open_gate():
    return _gate({"LITELLM_ANTHROPIC_ALLOWED_KEYS": ",".join(ALLOWED_KEYS)})


def test_sin_la_variable_nadie_pasa():
    gate = _gate({})
    for key in ALLOWED_KEYS + ("master", "claude-atlantic"):
        denied, detail = gate._anthropic_access_denied("anthropic-haiku-5-5", key)
        assert denied and detail["allowed"] == []


def test_solo_las_keys_de_nova(open_gate):
    for key in ALLOWED_KEYS:
        assert open_gate._anthropic_access_denied("anthropic-haiku-5-5", key) == (False, None)
    for key in ("master", "hermes", "claude-atlantic", "document-intake", None):
        assert open_gate._anthropic_access_denied("anthropic-haiku-5-5", key)[0], key


def test_no_toca_lo_que_no_es_anthropic(open_gate):
    for alias in ("claude-haiku-5-5", "gpt-6-luna", "qwen38-flash-next", "", None):
        assert open_gate._anthropic_access_denied(alias, "hermes") == (False, None)


def test_el_hook_llama_al_gate_y_el_deployment_lleva_la_lista():
    cm = next(d for d in _docs() if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    assert "_denied, _detail = _anthropic_access_denied(" in cm["data"]["litellm_strip_params.py"]
    dep = next(d for d in _docs() if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm")
    c = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "litellm")
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert set(env["LITELLM_ANTHROPIC_ALLOWED_KEYS"].split(",")) == set(ALLOWED_KEYS)
