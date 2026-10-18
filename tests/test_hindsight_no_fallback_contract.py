"""INFRA-940: la memoria de agentes (Hindsight) no degrada a la nube, igual que `brain`.

Se carga el hook REAL del manifiesto, como test_skirmbooks_ui_no_fallback_contract.py.
"""
from test_aurora_fallback_policy_contract import _policy, hook  # noqa: F401


def test_la_clave_de_hindsight_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "hindsight", {"disable_fallbacks": False})

    assert disabled is True
    assert request == {"disable_fallbacks": True}
    assert "hindsight" in hook.NO_FALLBACK_KEY_ALIASES
