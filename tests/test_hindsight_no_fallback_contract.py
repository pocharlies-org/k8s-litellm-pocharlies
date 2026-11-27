"""INFRA-940: la memoria de agentes (Hindsight) no degrada a la nube, igual que `brain`.

DGX-1904 (Dani, 27-11): salvo `gpt-6-luna`, que cae POR PETICION a Haiku 5.5 sin thinking
(`anthropic-haiku-5-5`, la API key de pago), para que el respaldo de Hindsight pase por LiteLLM.

Se carga el hook REAL del manifiesto, como test_skirmbooks_ui_no_fallback_contract.py.
"""
import pytest

from test_aurora_fallback_policy_contract import _policy, hook  # noqa: F401
from test_synapse_resident_fallback_contract import _router_fallbacks

LUNA = "gpt-6-luna"
HAIKU_SIN_THINKING = [{"model": "anthropic-haiku-5-5", "thinking": {"type": "disabled"}}]


def test_la_clave_de_hindsight_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "hindsight", {"disable_fallbacks": False})

    assert disabled is True
    assert request == {"disable_fallbacks": True}
    assert "hindsight" in hook.NO_FALLBACK_KEY_ALIASES


@pytest.mark.parametrize("model", ["qwen38-flash-next", "tooling", "bge-reranker", "anthropic-haiku-5-5", "gpt-6.2-sol"])
def test_hindsight_sigue_sellada_con_cualquier_otro_modelo(hook, model):  # noqa: F811
    request, disabled = _policy(hook, "hindsight", {"model": model})

    assert disabled is True
    assert "fallbacks" not in request


def test_hindsight_con_luna_cae_a_haiku_sin_thinking_y_el_cliente_no_lo_cambia(hook):  # noqa: F811
    request, disabled = _policy(hook, "hindsight", {"model": LUNA, "fallbacks": ["gpt-6.2-sol"]})

    assert disabled is False
    assert "disable_fallbacks" not in request
    assert request["fallbacks"] == HAIKU_SIN_THINKING
    request["fallbacks"][0]["thinking"]["type"] = "adaptive"  # el Router lo funde en sus kwargs
    assert _policy(hook, "hindsight", {"model": LUNA})[0]["fallbacks"] == HAIKU_SIN_THINKING


@pytest.mark.parametrize("key", ["hermes", "claude-atlantic", "nova"])
def test_ninguna_otra_key_cae_de_luna_a_la_key_de_pago(hook, key):  # noqa: F811
    request, disabled = _policy(hook, key, {"model": LUNA})

    assert disabled is False
    assert "fallbacks" not in request
    assert LUNA not in _router_fallbacks()
