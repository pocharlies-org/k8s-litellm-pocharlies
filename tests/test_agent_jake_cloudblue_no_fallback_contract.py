"""El copiloto de Agent Jake (perfil de trabajo de CloudBlue) no degrada a la nube.

Su key `agent-jake-cloudblue` solo pide `qwen38-flash-next`, que tiene una arista del
Router hacia `alibaba-q38-flash`; sellada con `disable_fallbacks`, con el residente caido
el contenido de CloudBlue falla a la vista en vez de salir a un tercero.
Se carga el hook REAL del manifiesto, como test_claude_cloudblue_no_fallback_contract.py.
"""
from test_aurora_fallback_policy_contract import _policy, hook  # noqa: F401


def test_la_key_del_copiloto_de_cloudblue_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "agent-jake-cloudblue", {"disable_fallbacks": False})

    assert disabled is True
    assert request == {"disable_fallbacks": True}
    assert "agent-jake-cloudblue" in hook.NO_FALLBACK_KEY_ALIASES


def test_la_key_sellada_pidiendo_el_residente_sigue_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "agent-jake-cloudblue", {"model": "qwen38-flash-next"})

    assert disabled is True
    assert request["disable_fallbacks"] is True
    assert "agent-jake-cloudblue" not in getattr(hook, "KEY_DIRECT_FALLBACK_MODELS", {})
