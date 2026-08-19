"""Quien puede llegar a la ruta abliterada, dicho en git y no en prosa.

POR QUE EXISTE (auditoria 19-08-2026). La model card publicada de este proyecto
pide tres cosas para λ>0, y el despliegue cumplia una:

    - λ>0 no debe compartir credenciales con herramientas de escritura
    - restringe el toolset con λ>0
    - manten /admin/refusal_lambda fuera del ingress publico   <- la unica cumplida

La key del gateway de OpenClaw (`openclaw-qwen36-prod`, team `openclaw`,
`models: []` o sea SIN restriccion, sin cuota y sin rpm) sirve `tooling` y
`tooling-uncensored` por igual, y la usa un agente CON herramientas de escritura.
Lo unico que mantiene censurado el trafico normal es que nadie pide el alias por su
nombre: una convencion, no un control.

LO QUE ESTE TEST SI PUEDE HACER: impedir que una concesion de modelos declarada en
git incluya la ruta abliterada por descuido. Las dos listas declarativas que hay
son `OPENCLAW_TEAM_REQUIRED_MODELS` (aqui) y `expected_models` del bootstrap de la
key de codex (en k8s-openclaw-qwen36-pocharlies). Ninguna debe concederla.

LO QUE NO PUEDE HACER, y queda para el owner: estrechar la key VIVA del gateway.
Hoy vale `models: []` y OpenClaw resuelve sus modelos del catalogo vivo con
comodin `litellm/*` sobre 54 alias, asi que enumerarlos mal es una caida. Esa es
una decision de disponibilidad contra aislamiento, no un detalle tecnico.
"""
import re
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# La ruta abliterada, en sus tres nombres.
ABLITERATED = frozenset({
    "tooling-uncensored",
    "deepseek-v4-flash-0731-uncensored",
    "qwen38-27b-uncensored",
})


def _sin_comentarios(text):
    """Quita comentarios antes de asertar.

    Estos bloques EXPLICAN en prosa justo lo que no deben contener ("`tooling-
    uncensored` NO entra aqui, y se intento"), asi que un assert sobre el texto
    crudo se dispara con la explicacion. Un test que falla por un comentario no
    mide nada.
    """
    return "\n".join(
        line.split("  #")[0] for line in text.splitlines()
        if not line.strip().startswith("#")
    )


def _team_required_models():
    text = MANIFEST.read_text()
    match = re.search(
        r'OPENCLAW_TEAM_REQUIRED_MODELS,\s*value:\s*"([^"]*)"', text
    ) or re.search(r'name:\s*OPENCLAW_TEAM_REQUIRED_MODELS[^}]*?value:\s*"([^"]*)"', text)
    assert match, "no encuentro OPENCLAW_TEAM_REQUIRED_MODELS en el manifiesto"
    return [m.strip() for m in match.group(1).split(",") if m.strip()]


def test_the_team_allowlist_never_grants_the_abliterated_route():
    """La lista que ensancha el equipo `openclaw` no puede incluirla.

    Ensanchar el equipo es lo contrario de aislar: daria la ruta abliterada a
    TODAS las keys del equipo de una vez, incluida la del agente con escritura.
    """
    granted = set(_team_required_models())
    assert not (granted & ABLITERATED), sorted(granted & ABLITERATED)
    # Guarda de cordura: la lista existe y concede lo normal, o el test de arriba
    # pasaria por estar vacia.
    assert "tooling" in granted


def test_no_abliterated_alias_is_reachable_without_naming_it():
    """Ningun alias NORMAL puede resolver a un destino abliterado.

    Es la otra mitad del aislamiento: da igual quien tenga permiso si `tooling`
    o un tier del router acaban sirviendo con el sello puesto. Lo comprueba sobre
    el mapa del hook, que es quien decide el destino.
    """
    text = MANIFEST.read_text()
    censored_map = _sin_comentarios(
        re.search(r"TOOLING_MODE_TARGETS = \{(.*?)\}", text, re.S).group(1))
    for alias in ABLITERATED:
        assert alias not in censored_map, (
            f"{alias} es destino del mapa CENSURADO: un `tooling` normal serviria "
            f"sin censura")


def test_the_abliterated_aliases_are_not_router_selectable():
    """Y el clasificador del router tampoco puede elegirlos.

    Si lo fuera, una peticion cualquiera podria volver sin censura sin haberla
    pedido — el aislamiento se rompe sin que nadie toque una key.
    """
    text = MANIFEST.read_text()
    tiers = _sin_comentarios(
        re.search(r"THINKING_TIERS = \{(.*?)\n    \}", text, re.S).group(1))
    route = re.search(r"\n    ROUTE = \{(.*?)\n    \}", text, re.S)
    for alias in ABLITERATED:
        assert alias not in tiers, f"{alias} es un tier de pensamiento"
        if route:
            assert alias not in _sin_comentarios(route.group(1)), (
                f"{alias} es destino del router")


@pytest.mark.parametrize("alias", sorted(ABLITERATED))
def test_every_abliterated_alias_stays_un_fallbacked(alias):
    """Sin red de nube. Un fallback aqui contesta censurado a quien pidio lo
    contrario, con HTTP 200 y sin aviso."""
    assert f"- {alias}: [" not in MANIFEST.read_text()


# El control compensatorio del dial global (lo vigila el watchdog porque no se
# puede prevenir: sin auth y con las NetworkPolicy sin aplicar) lo cubre
# test_uncensored_access_and_dial_alert_contract.py, que ademas comprueba la CLASE
# del aviso y que no tumbe el Job. No se duplica aqui.
