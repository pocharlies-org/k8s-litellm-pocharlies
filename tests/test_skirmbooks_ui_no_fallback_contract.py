"""SKIRM-183 (C2.2): las facturas de clientes de Skirmbooks no salen a la nube.

El OCR de facturas pide `tooling` con la clave virtual `skirmbooks-ui`. Sin sello, con
los residentes caídos `tooling` degradaba a `alibaba-q38-flash` (`TOOLING_FALLBACKS`) y
el texto de una factura de cliente viajaba a un tercero de pago. La decisión de SKIRM-177
(P2) es que esa clave esté en `NO_FALLBACK_KEY_ALIASES`: sin residente la llamada falla a
la vista (el workflow pasa la factura a `cancelled` con motivo «OCR cancelado») y el
redrive programado la recupera, en vez de sacarla de la máquina.

El alias es `skirmbooks-ui`, localizado el 17-10-2026: sha256 del `LITELLM_API_KEY` del
secret `skirmshop/skirmbooks-ui-secrets` (espejo ESO del ítem 1Password
`skirmshop-skirmbooks-ui`) contra el `token` de `/key/list` de LiteLLM. Es la misma clave
que comparten los adaptadores de Skirmbooks (`envFrom: skirmbooks-ui-secrets`), así que el
sello cubre al OCR y a los cinco pods de banking.

Se carga el hook REAL del manifiesto y se ejecutan solo sus funciones puras, igual que
`test_aurora_fallback_policy_contract.py` y `test_synapse_resident_fallback_contract.py`.
"""
import pytest

from test_aurora_fallback_policy_contract import READY_TP, _policy, hook  # noqa: F401
from test_session_routing_contract import _cfg  # noqa: F401
from test_session_routing_contract import configmap, docs, router_mod, router_src  # noqa: F401
from test_synapse_resident_fallback_contract import _cadena  # noqa: F401

ALIAS = "skirmbooks-ui"
PEDIDO = "tooling"  # LITELLM_OCR_MODEL del despliegue de Skirmbooks
NUBE = "alibaba-q38-flash"


def test_la_clave_de_skirmbooks_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, ALIAS)

    assert disabled is True
    assert request["disable_fallbacks"] is True
    assert ALIAS in hook.NO_FALLBACK_KEY_ALIASES


def test_el_body_del_cliente_no_deshace_el_sellado(hook):  # noqa: F811
    request, disabled = _policy(hook, ALIAS, {"disable_fallbacks": False})

    assert disabled is True
    assert request == {"disable_fallbacks": True}


def test_sin_residente_tooling_no_degrada_a_alibaba(hook):  # noqa: F811
    """C2.2: residente caído con Alibaba vivo = fallo visible, nunca el gemelo de la nube.

    Es la ruta que llevaba el OCR de una factura a `alibaba-q38-flash` (TOOLING_FALLBACKS).
    El `disable_fallbacks` es el que estampa la política de la clave, no uno puesto a mano:
    si la clave deja de estar sellada, este test cae con ella.
    """
    _, disabled = _policy(hook, ALIAS, {"model": PEDIDO})
    solo_nube_viva = lambda alias: alias == NUBE

    assert hook._tooling_route_for_state(
        READY_TP, solo_nube_viva, disable_fallbacks=disabled
    ) == (None, "dry", "compute_profile_target_unavailable")


@pytest.mark.parametrize("resident_ready", [True, False], ids=["residente-ready", "residente-caido"])
def test_el_session_router_no_la_mueve_a_alibaba(hook, router_mod, resident_ready):  # noqa: F811
    """Con lo peor que el session router sabe hacer (plan Alibaba, válvula, sticky,
    presupuesto KV apretado), una petición de esta clave se queda en `tooling`."""
    cfg = _cfg(sticky=True, instant_reject=True, local_slots=0, default_plan="alibaba")
    data, sellada, reescrita, env = _cadena(
        hook, router_mod, ALIAS, PEDIDO, cfg, resident_ready, inflight=99
    )

    assert sellada is True and data["disable_fallbacks"] is True
    assert reescrita is False and data["model"] == PEDIDO
    assert env.writes == []
    assert NUBE not in str(data)


@pytest.mark.parametrize("key_alias", ["hermes", "openclaw", "unknown"])
def test_las_demas_keys_siguen_sin_sellar(hook, key_alias):  # noqa: F811
    """El sello es solo de esta clave: el resto conserva su degradado a la nube."""
    request, disabled = _policy(hook, key_alias)

    assert disabled is False
    assert "disable_fallbacks" not in request
    assert key_alias not in hook.NO_FALLBACK_KEY_ALIASES
