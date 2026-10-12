"""DGX-841: Synapse cae a Alibaba SOLO cuando nombra el residente directo.

12-10-2026 (Dani): «lo que debería ser es que usara el modelo local y de fallback el
modelo Alibaba». La key `synapse` estaba sellada entera con `disable_fallbacks`
(decision del 20-09), de modo que el Router no aplicaba `qwen38-flash-next ->
alibaba-q38-flash` con el residente caido: 500 `Connection error` en vez de la
respuesta de Alibaba. Se abre ESE camino y ningun otro: `tooling`, `-uncensored` y
las demas keys selladas siguen igual.
"""
import pytest
import yaml

from test_aurora_fallback_policy_contract import MANIFEST, _policy, hook  # noqa: F401

RESIDENTE = "qwen38-flash-next"


def _router_fallbacks():
    config = next(
        yaml.safe_load(doc["data"]["config.yaml"])
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc
        and doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )
    return {
        source: destinations
        for edge in config["router_settings"].get("fallbacks") or []
        for source, destinations in edge.items()
    }


def test_las_excepciones_son_de_keys_selladas(hook):  # noqa: F811
    assert set(hook.KEY_DIRECT_FALLBACK_MODELS) <= hook.NO_FALLBACK_KEY_ALIASES


def test_synapse_con_el_residente_directo_no_se_sella(hook):  # noqa: F811
    request, disabled = _policy(hook, "synapse", {"model": RESIDENTE})

    assert disabled is False
    assert "disable_fallbacks" not in request


@pytest.mark.parametrize(
    "model", ["tooling", "qwen38-off", f"{RESIDENTE}-uncensored", "tooling-uncensored", "", "alibaba-q38-flash"]
)
def test_synapse_con_cualquier_otro_modelo_sigue_sellada(hook, model):  # noqa: F811
    request, disabled = _policy(hook, "synapse", {"model": model})

    assert disabled is True
    assert request["disable_fallbacks"] is True


def test_synapse_sin_modelo_en_el_body_sigue_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "synapse")

    assert disabled is True
    assert request == {"disable_fallbacks": True}


@pytest.mark.parametrize("key_alias", ["aurora-rca", "brain", "claude-cloudblue"])
def test_las_demas_keys_selladas_siguen_selladas_con_el_residente(hook, key_alias):  # noqa: F811
    request, disabled = _policy(hook, key_alias, {"model": RESIDENTE})

    assert disabled is True
    assert request["disable_fallbacks"] is True


def test_un_cliente_de_synapse_puede_pedir_no_caer(hook):  # noqa: F811
    request, disabled = _policy(
        hook, "synapse", {"model": RESIDENTE, "disable_fallbacks": True}
    )

    assert disabled is True
    assert request["disable_fallbacks"] is True


def test_el_router_solo_tiene_la_arista_del_residente():
    """C3: ni `tooling` ni ninguna ruta abliterada tienen salto en el Router."""
    graph = _router_fallbacks()

    assert graph == {RESIDENTE: ["alibaba-q38-flash"]}
