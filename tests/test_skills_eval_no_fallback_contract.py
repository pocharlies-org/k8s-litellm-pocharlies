"""SC-4384 (epica SC-1823, C5): el juez de skills mide contra el modelo registrado o falla.

La evaluacion de una skill (`tools/evaluar.py` de k8s-skills-catalog, pool `arc-skills-eval`)
pide el veredicto con `qwen38-flash-next` y la clave virtual `skills-eval`; `docs/evaluacion.md`
del catalogo fija ese modelo como el registrado. Sin sello, la clave podia acabar en
`alibaba-q38-flash` por dos caminos que su allowlist de Postgres no cierra (esa allowlist
gobierna lo que la key PUEDE PEDIR, no a donde DEGRADA despues):

* el session router (`session_router.py`): valvula de capacidad, sticky y plan por defecto
  reescriben el residente a su gemelo de la nube — medidos 42 en la ventana del run
  cancelado 41647405969;
* la arista del Router `qwen38-flash-next` -> `alibaba-q38-flash`
  (`router_settings.fallbacks`) y `TOOLING_FALLBACKS` cuando el residente no esta Ready.

Un veredicto medido con otro modelo no es una medida: es una medida falsa (mismo criterio que
`aurora-rca`, que tambien mide). La decision de SC-4384 es que `skills-eval` este en
`NO_FALLBACK_KEY_ALIASES`: se queda en el residente o falla a la vista, y el job `evaluar` no
publica ni «verificada» ni «retirada» con el modelo equivocado.

El alias es `skills-eval`, verificado el 03-11-2026 contra `/key/list` de LiteLLM:
`models: ["qwen38-flash-next", "tooling"]`.

Se carga el hook REAL del manifiesto y se ejecutan solo sus funciones puras, igual que
`test_aurora_fallback_policy_contract.py` y `test_skirmbooks_ui_no_fallback_contract.py`.
"""
import pytest

from test_aurora_fallback_policy_contract import READY_TP, _policy, hook  # noqa: F401
from test_session_routing_contract import _cfg  # noqa: F401
from test_session_routing_contract import configmap, docs, router_mod, router_src  # noqa: F401
from test_synapse_resident_fallback_contract import _cadena  # noqa: F401

ALIAS = "skills-eval"
RESIDENTE = "qwen38-flash-next"   # el modelo que fija docs/evaluacion.md del catalogo
PEDIDO = "tooling"                 # el otro alias de su allowlist (mismo residente)
NUBE = "alibaba-q38-flash"


def test_la_clave_del_juez_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, ALIAS)

    assert disabled is True
    assert request["disable_fallbacks"] is True
    assert ALIAS in hook.NO_FALLBACK_KEY_ALIASES


def test_no_tiene_excepcion_que_la_deshaga(hook):  # noqa: F811
    """`KEY_DIRECT_FALLBACK_MODELS` es el unico hueco del sello; la clave del juez no lo usa."""
    assert ALIAS not in hook.KEY_DIRECT_FALLBACK_MODELS


@pytest.mark.parametrize("pedido", [RESIDENTE, PEDIDO])
def test_los_dos_alias_de_su_allowlist_van_sellados(hook, pedido):  # noqa: F811
    """Los unicos dos modelos que la clave puede pedir (medido en /key/list) salen sellados."""
    request, disabled = _policy(hook, ALIAS, {"model": pedido})

    assert disabled is True
    assert request["disable_fallbacks"] is True


def test_el_body_del_cliente_no_deshace_el_sellado(hook):  # noqa: F811
    request, disabled = _policy(hook, ALIAS, {"disable_fallbacks": False})

    assert disabled is True
    assert request == {"disable_fallbacks": True}


def test_sin_residente_no_degrada_a_alibaba(hook):  # noqa: F811
    """Residente caido con la nube viva = fallo visible, nunca el gemelo de Alibaba.

    Es la arista `router_settings.fallbacks` / `TOOLING_FALLBACKS`. El `disable_fallbacks`
    es el que estampa la politica de la clave: si la clave deja de estar sellada, este test
    cae con ella.
    """
    _, disabled = _policy(hook, ALIAS, {"model": PEDIDO})
    solo_nube_viva = lambda alias: alias == NUBE  # noqa: E731

    assert hook._tooling_route_for_state(
        READY_TP, solo_nube_viva, disable_fallbacks=disabled
    ) == (None, "dry", "compute_profile_target_unavailable")


@pytest.mark.parametrize("pedido", [RESIDENTE, PEDIDO])
@pytest.mark.parametrize("resident_ready", [True, False], ids=["residente-ready", "residente-caido"])
def test_el_session_router_no_la_mueve_a_alibaba(hook, router_mod, pedido, resident_ready):  # noqa: F811
    """Con lo peor que el session router sabe hacer (plan Alibaba, valvula, sticky,
    presupuesto KV apretado), una peticion del juez se queda en el residente pedido."""
    cfg = _cfg(sticky=True, instant_reject=True, local_slots=0, default_plan="alibaba")
    data, sellada, reescrita, env = _cadena(
        hook, router_mod, ALIAS, pedido, cfg, resident_ready, inflight=99
    )

    assert sellada is True and data["disable_fallbacks"] is True
    assert reescrita is False and data["model"] == pedido
    assert env.writes == []
    assert NUBE not in str(data)


@pytest.mark.parametrize("key_alias", ["hermes", "openclaw", "unknown"])
def test_las_demas_keys_siguen_sin_sellar(hook, key_alias):  # noqa: F811
    """El sello es solo de esta clave: el resto conserva su degradado a la nube."""
    request, disabled = _policy(hook, key_alias)

    assert disabled is False
    assert "disable_fallbacks" not in request
    assert key_alias not in hook.NO_FALLBACK_KEY_ALIASES
