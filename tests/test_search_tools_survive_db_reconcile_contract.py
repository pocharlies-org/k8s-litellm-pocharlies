"""Si el config declara `search_tools`, el reconcile de BD NO las puede gestionar.

POR QUE EXISTE (2026-09-16, WebSearch de Claude Code caia a Perplexity)
----------------------------------------------------------------------
El sintoma, tal cual lo vio Claude Code:

    litellm.APIConnectionError: PerplexityException - PERPLEXITYAI_API_KEY is not set

con `search_tools: searxng-local` escrito en el ConfigMap desde hace semanas y
SearXNG vivo. El bloque estaba, el ConfigMap aplicado era ese, y aun asi el Router
se quedaba sin ninguna herramienta de busqueda.

El mecanismo: con `general_settings.store_model_in_db: false` LiteLLM lanza
`reload_search_tools_from_db` al arrancar y **cada 30 segundos**. Ese trabajo
reconstruye `router.search_tools` desde

    parse_search_tools(self.get_config_state())      # proxy_server.py:7774

o sea desde el config **en memoria** — no desde el fichero —, obtenia 0
herramientas y **asignaba la lista vacia**, borrando la del arranque. Sin
herramienta, el callback `websearch_interception` cae al provider por defecto
(`handler.py:1399-1405`, `search_provider = "perplexity"`) y ahi no hay key.

La salida que upstream documenta como opt-out (`proxy_server.py:7807`) es
`general_settings.supported_db_objects`: una **allowlist de cadenas exactas**
(`proxy_server.py:4400`) de los tipos que el reconcile puede tocar. Sacar de esa
lista `search_tools` deja la herramienta en manos del config y fuera del reconcile.

LO QUE ROMPE EL ARREGLO, y por eso este test y no un comentario
----------------------------------------------------------------
Dos formas, y las dos son silenciosas:

  1. Alguien "ordena" el bloque y devuelve `search_tools` a la allowlist. Vuelve
     el bug tal cual, con el ConfigMap perfecto en git.

  2. La lista es un **enumerate**: hay que escribir los 16 tipos que el paquete
     consulta. Al subir version de litellm, un tipo nuevo que falte aqui se carga
     a `false` en silencio — o sea desactivado — y nadie se entera hasta que algo
     deja de funcionar.

El punto 2 no se puede comprobar desde fuera (en CI no hay paquete litellm
instalado, solo PyYAML), asi que aqui se clava el conjunto: si alguien sube
litellm y rehace la lista contra los `should_load_db_object(object_type=...)` del
paquete, tiene que pasar por este fichero. El fallo es un recordatorio, no un
portazo.

El `por que` de fondo sigue sin estar resuelto: por que el estado en memoria del
proceso vivo pierde la clave top-level `search_tools` no se reproduce fuera de el
(relanzar `load_config` a mano contra el mismo fichero no lo hace) y upstream
`main` sigue igual. El arreglo no depende de saberlo.
"""
import pathlib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
WATCHDOG = ROOT / "k8s" / "litellm-watchdog-cron.yaml"

# Los 16 tipos que litellm v1.100.0 consulta con should_load_db_object(). Sin
# `search_tools` a proposito: ver el docstring. Se rehace ENTERA al subir version,
# grepeando el paquete instalado:
#   python -c "import litellm,os,re; ..."  # should_load_db_object(object_type="X")
TIPOS_GESTIONADOS_POR_BD = {
    "models", "mcp", "guardrails", "policies", "vector_stores",
    "vector_store_indexes", "pass_through_endpoints", "prompts",
    "model_cost_map", "tools", "config_overrides", "agents",
    "anthropic_beta_headers", "cache_settings", "semantic_filter_settings",
    "sso_settings",
}


def _config():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def test_si_el_config_declara_search_tools_el_reconcile_no_las_toca():
    """El contrato en una frase: quien declara la herramienta de busqueda es el
    config, luego el reconcile de BD no puede ser quien la escriba en el Router."""
    cfg = _config()
    declaradas = cfg.get("search_tools") or []
    if not declaradas:
        pytest_skip_sin_config()          # sin herramienta declarada no hay nada que cuidar
    allow = (cfg.get("general_settings") or {}).get("supported_db_objects")
    assert allow is not None, (
        "no hay `general_settings.supported_db_objects`: sin allowlist explicita "
        "litellm gestiona TODOS los tipos desde la BD, incluido `search_tools`, y "
        "el reconcile de cada 30 s vacia `router.search_tools` desde el config en "
        "memoria. Es exactamente el estado del 16-09: WebSearch caia a Perplexity "
        "con searxng-local en el ConfigMap."
    )
    assert "search_tools" not in allow, (
        "`search_tools` ha vuelto a `supported_db_objects`. Con `store_model_in_db: "
        "false` el job `reload_search_tools_from_db` (cada 30 s) reconstruye "
        "`router.search_tools` desde `parse_search_tools(self.get_config_state())`, "
        "obtiene 0 y asigna la lista vacia; sin herramienta, "
        "`websearch_interception` cae a `search_provider = \"perplexity\"` "
        "(handler.py:1399-1405) y Claude Code ve `PERPLEXITYAI_API_KEY is not "
        "set`. Saala de la lista."
    )


def test_la_allowlist_es_el_conjunto_clavado_sin_sobrantes_ni_faltantes():
    """Un tipo que falte se carga a `false` en silencio: no es "no lo toco", es
    "desactivado". Por eso comparo por igualdad y no por inclusion."""
    allow = (_config().get("general_settings") or {}).get("supported_db_objects") or []
    assert set(allow) == TIPOS_GESTIONADOS_POR_BD, (
        "la allowlist ha divergido del conjunto clavado.\n"
        "  de mas: %s\n  de menos: %s\n"
        "Si has subido version de litellm: rehaz la lista contra los "
        "`should_load_db_object(object_type=...)` del paquete instalado y actualiza "
        "TIPOS_GESTIONADOS_POR_BD aqui. Un tipo nuevo que falte se carga a false en "
        "silencio (desactivado), que es justo el fallo que este test quiere que se "
        "vea en CI y no en produccion." % (
            sorted(set(allow) - TIPOS_GESTIONADOS_POR_BD),
            sorted(TIPOS_GESTIONADOS_POR_BD - set(allow)),
        )
    )


def test_el_vigilante_pregunta_por_la_herramienta_que_declaro_el_config():
    """El watchdog del CronJob comprueba que el Router tiene `searxng-local`. Si
    aqui se renombra la herramienta y alla se deja el nombre viejo, el vigilante
    deja de avisar sin decir nada: otro semaforo que miente por deriva de nombres."""
    declaradas = {t.get("search_tool_name") for t in (_config().get("search_tools") or [])}
    assert declaradas == {"searxng-local"}, (
        "el config ya no declara `searxng-local` (%s): actualiza el nombre que "
        "espera el watchdog en k8s/litellm-watchdog-cron.yaml y este test." % declaradas)
    texto = WATCHDOG.read_text()
    assert "searxng-local" in texto, (
        "el watchdog ya no menciona `searxng-local`: ha dejado de comprobar que el "
        "Router tiene buscador, y eso es invisible hasta que alguien usa WebSearch")


def pytest_skip_sin_config():
    """Sin `search_tools` en el config no hay contrato que vigilar. Se deja escrito
    en lugar de callar: si algun dia se quita la herramienta, esto tiene que decir
    que el vigilante y la allowlist tambien dejan de tener sentido."""
    import pytest
    pytest.skip("el config ya no declara `search_tools`: no hay nada que cuidar")
