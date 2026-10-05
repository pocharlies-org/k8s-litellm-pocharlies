"""claude-cloudblue (Claude Code del Mac, datos de CloudBlue) nunca degrada a Alibaba.

`disable_fallbacks` se estampa en el pre-call hook para esa key, con lo que ni el
fallback del Router (qwen38-flash-next -> alibaba-q38-flash) ni el overflow del
session_router pueden sacar el prompt a un tercero. Decision del owner, 05-10-2026.
"""
import pytest

from test_aurora_fallback_policy_contract import _policy, hook  # noqa: F401


def test_claude_cloudblue_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "claude-cloudblue")

    assert disabled is True
    assert request["disable_fallbacks"] is True
    assert "claude-cloudblue" in hook.NO_FALLBACK_KEY_ALIASES


def test_el_body_del_cliente_no_deshace_el_sellado(hook):  # noqa: F811
    request, disabled = _policy(
        hook, "claude-cloudblue", {"disable_fallbacks": False}
    )

    assert disabled is True
    assert request == {"disable_fallbacks": True}


@pytest.mark.parametrize("key_alias", ["claude-local", "hermes", "unknown"])
def test_otras_keys_no_quedan_selladas(hook, key_alias):  # noqa: F811
    request, disabled = _policy(hook, key_alias)

    assert disabled is False
    assert "disable_fallbacks" not in request
    assert key_alias not in hook.NO_FALLBACK_KEY_ALIASES
