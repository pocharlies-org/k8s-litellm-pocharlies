"""SC-2917 (problema SC-2913): `harness-clasificador`, el residente corto del clasificador.

El clasificador de permisos del harness (Stage 2) llamaba al residente directo
`qwen38-flash-next`: con los slots llenos el semaforo `max_parallel_requests` rechaza con
429 en vez de encolar, y el timeout de 240 s deja un slot ocupado 4 minutos. El 429 caia
al respaldo (Alibaba, muerto por quota), el clasificador se quedaba sin veredicto y el
harness lo presentaba como denegacion de seguridad (dos sesiones paradas el 23-10).
Mismo diseno que `correo-clasificador` (DGX-874): id propio = semaforo propio, timeout
corto, sin reintento. A diferencia de aquel, SIN arista de fallback: ampliar el grafo es
decision del owner (test_tooling_profile_contract).

Lo que solo se ve en vivo (la allowlist `models` de la key `claude-local` en la Postgres)
no es un test: es el paso de operacion de la Request.
"""
import yaml

from test_aurora_fallback_policy_contract import MANIFEST
from test_family_sampling_contract import hook as sampling_hook  # noqa: F401

ALIAS = "harness-clasificador"
RESIDENTE = "qwen38-flash-next"


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
    """Todo salvo `timeout` y `num_retries` es el del residente TP=2 por ancla: si el
    residente cambia, el alias lo sigue. Una copia que se desvia es como dos nombres
    que dejan de significar lo mismo."""
    residente = next(
        e for e in _entradas(RESIDENTE)
        if (e.get("model_info") or {}).get("id") == "qwen38-flash-next-tp2"
    )
    propios = {"timeout", "num_retries"}

    def sin_propios(params):
        return {k: v for k, v in params.items() if k not in propios}

    assert sin_propios(_alias()["litellm_params"]) == sin_propios(residente["litellm_params"])


def test_timeout_corto_y_sin_reintento_del_mismo_deployment():
    """El slot no se ocupa 4 minutos: 6 s y fuera, sin reintento del Router (serian 2 x 6)."""
    params = _alias()["litellm_params"]

    assert params["timeout"] <= 8
    assert params["num_retries"] == 0


def test_id_propio_y_sello_censurado_heredados():
    """El id propio es la llave del semaforo y del cooldown (DGX-852); el `refusal:0`
    viene de la ancla: sin el, el residente contesta ABLADO."""
    info = _alias()["model_info"]

    assert info["id"] == ALIAS
    assert _alias()["litellm_params"]["extra_body"]["cache_salt"] == "refusal:0"
    assert info["cooldown_time"] == 60
    assert list(info["supported_reasoning_efforts"]) == ["none"]


def test_sin_arista_de_fallback():
    """El grafo de fallbacks es decision del owner (PERMITIDAS en
    test_tooling_profile_contract): este alias no tiene, y no se le pone por debajo."""
    fallbacks = {
        origen
        for arista in _config()["router_settings"]["fallbacks"]
        for origen in arista
    }

    assert ALIAS not in fallbacks


def test_las_tablas_del_hook_no_conocen_el_alias(sampling_hook):  # noqa: F811
    """Ni THINKING_TIERS ni SWAPPABLE_ALIASES: entrar en la segunda le pondria el
    sampling del residente a una peticion que no lleve esquema."""
    assert sampling_hook.THINKING_TIERS.get(ALIAS) is None
    assert ALIAS not in sampling_hook.SWAPPABLE_ALIASES
