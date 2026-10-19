"""DGX-874 (C1): la key `synapse` solo sale a Alibaba por la arista del Router, y solo con `correo-clasificador`.

12-10-2026 (Dani, DGX-841): «lo que debería ser es que usara el modelo local y de fallback el
modelo Alibaba». La key `synapse` estaba sellada entera con `disable_fallbacks`, y con el residente
caido el Router no aplicaba `qwen38-flash-next -> alibaba-q38-flash`. DGX-841 abrio ESE camino
sacando `qwen38-flash-next` del sello, y con el abrio otro que nadie decidio: una peticion sin sello
la reescribe el session router del hook (plan por defecto, valvula `instant_reject`, presupuesto KV,
sticky, re-bind). El 13-10 fue el 78 % de las peticiones de la key, todas con `attempted_fallbacks=0`
(nota de security de DGX-874, «Lo medido»).

C1 de security, opcion (a): `qwen38-flash-next` vuelve al sello. Lo unico que sigue sin sellar es
`correo-clasificador` (DGX-874 P1), que no esta en ROUTED_MODELS: el session router no lo toca y su
unico camino a Alibaba es la arista del Router, cuando el residente falla.
"""
import ast

import pytest
import yaml

from test_aurora_fallback_policy_contract import MANIFEST, _hook_source, _policy, hook  # noqa: F401
from test_session_routing_contract import _Env, _cfg, _data  # noqa: F401
from test_session_routing_contract import configmap, docs, router_mod, router_src  # noqa: F401

RESIDENTE = "qwen38-flash-next"
ALIAS = "correo-clasificador"
NUBE = "alibaba-q38-flash"


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


def _cadena(hook, router_mod, key, modelo, cfg, resident_ready=True, **entorno):  # noqa: F811
    """Lo que el hook real hace con una peticion de `key`: primero la politica de fallbacks por key
    (estampa el sello) y despues el session router, en ese orden
    (test_el_sello_se_estampa_antes_del_session_router)."""
    data = _data(model=modelo, metadata={"user_api_key_alias": key})
    sellada = hook._apply_key_fallback_policy(data, {"key_alias": key})
    env = _Env(router_mod, cfg, **entorno)
    reescrita = env.run(data, requested=modelo, resident_ready=resident_ready)
    return data, sellada, reescrita, env


def test_las_excepciones_son_de_keys_selladas(hook):  # noqa: F811
    assert set(hook.KEY_DIRECT_FALLBACK_MODELS) <= hook.NO_FALLBACK_KEY_ALIASES


def test_la_unica_excepcion_de_synapse_es_el_alias_del_clasificador(hook):  # noqa: F811
    assert hook.KEY_DIRECT_FALLBACK_MODELS == {"synapse": frozenset({ALIAS})}


@pytest.mark.parametrize(
    "model",
    [RESIDENTE, "tooling", "qwen38-off", f"{RESIDENTE}-uncensored", "tooling-uncensored", "", NUBE],
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
    """Con el alias, el unico que la key deja sin sello: el cliente puede sellarlo el."""
    request, disabled = _policy(hook, "synapse", {"model": ALIAS, "disable_fallbacks": True})

    assert disabled is True
    assert request["disable_fallbacks"] is True


def test_el_sello_se_estampa_antes_del_session_router():
    """Si el session router corriera antes, miraria una peticion todavia sin sello."""
    hook_fn = next(
        n
        for n in ast.walk(ast.parse(_hook_source()))
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_pre_call_hook"
    )

    def linea(nombre):
        return next(
            c.lineno
            for c in ast.walk(hook_fn)
            if isinstance(c, ast.Call) and getattr(c.func, "id", getattr(c.func, "attr", None)) == nombre
        )

    assert linea("_apply_key_fallback_policy") < linea("apply_session_routing")


@pytest.mark.parametrize(
    "cfg, resident_ready, entorno",
    [
        (dict(sticky=True, instant_reject=True, default_plan="alibaba"), True, {}),
        (dict(sticky=True, instant_reject=True, local_slots=2), True, {"inflight": 99}),
        (dict(sticky=True), True, {"bound": "alibaba"}),
        (dict(sticky=True, instant_reject=True), False, {"bound": "local"}),
    ],
    ids=["plan-por-defecto-alibaba", "valvula-instant-reject", "sticky-a-alibaba", "residente-no-ready"],
)
def test_synapse_con_el_residente_directo_no_la_mueve_el_session_router(  # noqa: F811
    hook, router_mod, cfg, resident_ready, entorno
):
    """C1: el plan por defecto, la valvula, el sticky y el re-bind ya no alcanzan a la key. Con el
    residente Ready la sirve el; con el residente caido falla a la vista (y el bucle de
    reclasificacion del adapter lo reintenta) en vez de salir a Alibaba por un camino sin decidir."""
    data, sellada, reescrita, env = _cadena(
        hook, router_mod, "synapse", RESIDENTE, _cfg(**cfg), resident_ready, **entorno
    )

    assert sellada is True and data["disable_fallbacks"] is True
    assert reescrita is False and data["model"] == RESIDENTE
    assert env.writes == []


@pytest.mark.parametrize("resident_ready", [True, False], ids=["residente-ready", "residente-caido"])
def test_el_clasificador_solo_llega_a_alibaba_por_la_arista_del_router(  # noqa: F811
    hook, router_mod, resident_ready
):
    """Sin sello, para que el Router pueda aplicar su arista; y con todo lo que el session router
    sabe hacer encendido, ni plan, ni valvula, ni sticky tocan una peticion al alias."""
    cfg = _cfg(sticky=True, instant_reject=True, local_slots=0, default_plan="alibaba")
    data, sellada, reescrita, env = _cadena(
        hook, router_mod, "synapse", ALIAS, cfg, resident_ready, inflight=99
    )

    assert sellada is False and "disable_fallbacks" not in data
    assert reescrita is False and data["model"] == ALIAS
    assert env.writes == []
    assert _router_fallbacks()[ALIAS] == [NUBE]


@pytest.mark.parametrize("key", ["hermes", "claude-x86"])
def test_las_keys_que_nunca_estuvieron_selladas_siguen_igual(hook, router_mod, key):  # noqa: F811
    """El cambio es de `synapse`: el resto de keys, con el residente directo, va sin sello y el
    session router sigue pudiendo moverlas."""
    cfg = _cfg(sticky=True, instant_reject=True, default_plan="alibaba")
    data, sellada, reescrita, _ = _cadena(hook, router_mod, key, RESIDENTE, cfg)

    assert sellada is False and "disable_fallbacks" not in data
    assert reescrita is True and data["model"] == NUBE


def test_el_router_solo_tiene_las_aristas_del_residente_y_del_clasificador():
    """C3: ni `tooling` ni ninguna ruta abliterada tienen salto en el Router.

    13-10-2026 (DGX-874): segunda arista, `correo-clasificador` (el mismo residente con
    timeout 6 para el clasificador de correo de Synapse) al mismo gemelo de Alibaba. La del
    residente la siguen usando las keys sin sello; `synapse` ya no.
    """
    graph = _router_fallbacks()

    assert graph == {
        RESIDENTE: [NUBE],
        ALIAS: [NUBE],
    }
