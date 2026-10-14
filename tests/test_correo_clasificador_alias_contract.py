"""DGX-874 (P1): `correo-clasificador`, el residente con respaldo CORTO a Alibaba.

El 12-10 el adapter de Gmail de Synapse cortaba a los 30 s (`classifier_timeout_s`) y
`qwen38-flash-next` solo suelta a su respaldo a los 240 s: la arista
`qwen38-flash-next -> alibaba-q38-flash` existe y nunca llegaba a tiempo (182 intentos
fallidos para 37 correos). El alias apunta al MISMO Service del residente, con un
`timeout` corto y sin reintento del mismo deployment, y cae a `alibaba-q38-flash`.

Aqui se fija lo que P1 deja escrito en git. Lo que solo se ve en vivo (la allowlist de la
key, lo que devuelve Alibaba) es el paso L1 de la historia, no un test.
"""
import pytest
import yaml

from test_aurora_fallback_policy_contract import MANIFEST, _policy
from test_aurora_fallback_policy_contract import hook as policy_hook  # noqa: F401
from test_family_sampling_contract import _install_fake_litellm
from test_family_sampling_contract import hook as sampling_hook  # noqa: F401

ALIAS = "correo-clasificador"
RESIDENTE = "qwen38-flash-next"
NUBE = "alibaba-q38-flash"


def _config():
    return next(
        yaml.safe_load(doc["data"]["config.yaml"])
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc
        and doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )


def _entradas(nombre):
    return [m for m in _config()["model_list"] if m["model_name"] == nombre]


def _alias():
    entradas = _entradas(ALIAS)
    assert len(entradas) == 1, (
        f"`{ALIAS}` tiene que ser UNA deployment: con dos bajo un alias la que esta "
        f"muerta se come peticiones hasta acumular los fallos del cooldown; hay {len(entradas)}"
    )
    return entradas[0]


def test_el_alias_es_el_mismo_service_del_residente_y_no_una_copia():
    """Todo salvo `timeout` y `num_retries` es el del residente: api_base, modelo, tope de
    paralelo y el sello `cache_salt` llegan por ancla, y si el residente cambia el alias
    lo sigue. Una copia que se desvia es justo como dos nombres dejan de significar lo mismo."""
    # DGX-852: el residente son dos deployments (TP=2 y TP=1) bajo el mismo nombre.
    # El clasificador hereda del PAR TP=2 y ahi se queda: es una llamada corta y
    # latencia-critica cuyo contexto vive en la caché de prefijo de ese motor, y el
    # ancla de la que hereda es la del residente censurado de siempre.
    residente = next(
        e for e in _entradas(RESIDENTE)
        if (e.get("model_info") or {}).get("id") == "qwen38-flash-next-tp2"
    )
    propios = {"timeout", "num_retries"}

    def sin_propios(params):
        return {k: v for k, v in params.items() if k not in propios}

    assert sin_propios(_alias()["litellm_params"]) == sin_propios(residente["litellm_params"])


def test_timeout_corto_y_sin_reintento_del_mismo_deployment():
    """`num_retries` 0: sin el, el Router (`router_settings.num_retries` 1) reintenta el
    mismo deployment antes de caer al respaldo, o sea 2 x timeout."""
    params = _alias()["litellm_params"]

    assert params["timeout"] <= 8
    assert params["num_retries"] == 0


def test_el_sello_censurado_no_se_pierde():
    """Viene de la ancla. Sin `refusal:0` el residente (lambda global 1.0) contesta ABLADO."""
    assert _alias()["litellm_params"]["extra_body"]["cache_salt"] == "refusal:0"


def test_aparca_corto_y_solo_anuncia_none():
    """A diferencia del residente (900 s), aqui el prompt es corto y Alibaba es barato: no
    hay razon para apartarlo 15 min. Y no piensa: el menu publicado es solo `none`."""
    info = _alias()["model_info"]

    assert info["cooldown_time"] == 60
    assert list(info["supported_reasoning_efforts"]) == ["none"]


def test_el_respaldo_es_el_gemelo_cloud():
    config = _config()
    fallbacks = {
        origen: destinos
        for arista in config["router_settings"]["fallbacks"]
        for origen, destinos in arista.items()
    }

    assert fallbacks[ALIAS] == [NUBE]
    assert NUBE in {m["model_name"] for m in config["model_list"]}


def test_synapse_con_el_alias_no_se_sella(policy_hook):  # noqa: F811
    """Sin la excepcion en KEY_DIRECT_FALLBACK_MODELS, `disable_fallbacks` cortaria la arista."""
    request, disabled = _policy(policy_hook, "synapse", {"model": ALIAS})

    assert disabled is False
    assert "disable_fallbacks" not in request


def test_synapse_con_tooling_sigue_sellada(policy_hook):  # noqa: F811
    request, disabled = _policy(policy_hook, "synapse", {"model": "tooling"})

    assert disabled is True
    assert request["disable_fallbacks"] is True


@pytest.mark.parametrize("key_alias", ["aurora-rca", "brain", "claude-cloudblue"])
def test_las_demas_keys_selladas_siguen_selladas_con_el_alias(policy_hook, key_alias):  # noqa: F811
    request, disabled = _policy(policy_hook, key_alias, {"model": ALIAS})

    assert disabled is True
    assert request["disable_fallbacks"] is True


@pytest.mark.parametrize("con_esquema", [True, False])
def test_la_peticion_real_del_adapter_llega_sin_pensar_y_con_temperatura_cero(
    sampling_hook, con_esquema  # noqa: F811
):
    """El cuerpo es el de `_call_llm` del adapter. `reasoning_effort: none` se traduce a
    `enable_thinking: false` sea cual sea el alias; el sampling por familia solo pisa los
    alias de SWAPPABLE_ALIASES, y este no esta, asi que la temperatura 0 llega intacta con
    o sin `response_format`."""
    _install_fake_litellm({ALIAS: [_alias()]})
    data = {
        "model": ALIAS,
        "reasoning_effort": "none",
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": "Clasifica el correo."},
            {"role": "user", "content": "remitente, asunto y cuerpo"},
        ],
    }
    if con_esquema:
        data["response_format"] = {"type": "json_object"}

    sampling_hook._apply_family_sampling(data, ALIAS)
    sampling_hook._apply_thinking_tier(data, ALIAS)

    assert data["temperature"] == 0
    assert "top_p" not in data
    assert data["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert "reasoning_effort" not in data


def test_las_tablas_del_hook_no_conocen_el_alias(sampling_hook):  # noqa: F811
    """El alias no entra en THINKING_TIERS ni en SWAPPABLE_ALIASES: entrar en la segunda le
    pondria el sampling del residente (temperature 0.7) a una peticion que no lleve esquema."""
    assert ALIAS not in sampling_hook.THINKING_TIERS
    assert ALIAS not in sampling_hook.SWAPPABLE_ALIASES
