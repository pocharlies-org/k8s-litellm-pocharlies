"""Contrato de la superficie `/v1/messages` medida en la auditoria SC-326.

POR QUE EXISTE ESTE TEST (2026-09-07, SC-324 / SC-329)
-----------------------------------------------------
La epica SC-324 audito la superficie Anthropic `/v1/messages` de LiteLLM (imagen
v1.96.0) contra el residente `qwen38-flash-next`, llamada a llamada, contra el
endpoint vivo (SC-326, comentario Jira 10914). Salio un semaforo: varios grupos
VERDE y dos ROJO con su issue upstream. El doc `doc/anthropic-compat.md` transcribe
ese estado.

El problema es el de siempre en este repo: `Synced / Healthy` de ArgoCD significa
"el manifiesto aplicado coincide con git", NO "el comportamiento sigue vivo". Un
`model_info` que pierde `supports_vision`, un callback que se cae de la lista o el
flag de ruta que migra de sitio convierten un VERDE medido en un fallo silencioso
de Claude Code, y nadie se entera hasta que un usuario pierde una capability.

Este archivo congela la MITAD DE MANIFIESTO de cada grupo que quedo VERDE: la
pieza concreta del yaml que habilita ese comportamiento. Cada test falla si esa
pieza desaparece o se renombra. La otra mitad (la peticion real y su respuesta)
se midio el 07-09 contra el proxy vivo y su evidencia esta en SC-326; la
reproduccion copia/pegable esta en `doc/anthropic-compat.md`.

NO confunde esto con `tests/test_anthropic_messages_reasoning_contract.py` (SC-203,
el default `low` del thinking). Ese contrato mira el HOOK; este mira el MANIFIESTO
y cubre los grupos verdes de SC-326 que el del hook no toca: tools, tool_choice,
streaming, vision, count_tokens y websearch.
"""
import pathlib

import yaml


MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# El residente auditado por SC-326. Si el residente llm-tp cambia de alias, este
# archivo se actualiza a la vez que `doc/anthropic-compat.md` — el doc y el
# contrato describen el MISMO residente medido.
RESIDENTE = "qwen38-flash-next"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _config() -> dict:
    for doc in _docs():
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def _deployment(nombre):
    for doc in _docs():
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == nombre:
            return doc
    raise AssertionError(f"no encuentro el Deployment {nombre}")


def _container(deployment, nombre):
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == nombre:
            return c
    raise AssertionError(f"no encuentro el contenedor {nombre}")


def _entrada(alias: str) -> dict:
    for e in _config()["model_list"]:
        if e["model_name"] == alias:
            return e
    raise AssertionError(f"no encuentro {alias} en el model_list")


# ── Grupo 1 de SC-326: tools multi-turno (VERDE) ──────────────────────────────


def test_el_residente_declara_function_calling():
    """SC-326 grupo 1: el round-trip del tool id (chatcmpl-tool-...) y la
    continuacion con tool_result salieron VERDES. Que el residente ANUNCIE
    `supports_function_calling: true` es lo que hace que el hook de capacidad
    deje pasar una peticion con tools en vez de desviarla o rechazarla: si el
    booleano se va o se pasa a false, el verde de SC-326 se rompe en silencio
    para Claude Code (que manda tools en casi todos los turnos)."""
    info = _entrada(RESIDENTE).get("model_info") or {}
    assert info.get("supports_function_calling") is True


def test_el_residente_es_un_backend_openai_compat_con_api_base_propia():
    """SC-326 grupos 1-4 y 6: la auditoria entero (tools, tool_choice, streaming)
    discurrio por la ruta chat-completions del provider `openai/` contra el
    service del cluster. La traduccion Anthropic->OpenAI del adaptador
    (`translate_anthropic_tool_choice_to_openai`, tool_choice auto/tool) Y el
    round-trip de tool ids que salio verde dependen de las dos mitades de esta
    entrada: el prefijo `openai/` (que es lo que selecciona el adaptador) y el
    `api_base` al service llm (que es lo que la mantiene en casa). Renombrar el
    alias o cambiar el provider a p.ej. `hosted_vllm/` cambia el camino de
    traduccion medido — no es un detalle estetico, es otro experimento."""
    e = _entrada(RESIDENTE)
    params = e["litellm_params"]
    assert params["model"] == f"openai/{RESIDENTE}"
    assert params["api_base"].startswith(f"http://{RESIDENTE}.llm.svc.cluster.local")


# ── Grupos 2/4/6: tool_choice auto, tool especifico, streaming (VERDE) ───────


def test_la_ruta_de_chat_completions_para_anthropic_sigue_activa():
    """SC-326 grupos 2, 4 y 6: tool_choice auto/tool y el streaming (con y sin
    tools, `input_json_delta` completo, cero deltas duplicados) se midieron por
    la ruta de chat completions, que es la que habilita el flag del contenedor
    PRINCIPAL. Sin el, /v1/messages va por la ruta de responses y NINGUNO de
    esos verdes se sostiene (SC-203 lo documento para el thinking; SC-326 lo
    confirmo para tools y streaming). El contrato hermano de SC-203 ya lo fija
    para el razonamiento; se fija aqui TAMBIEN porque para esta epica el flag es
    la condicion de los tres grupos verdes de tools/streaming, y un contrato por
    grupo es lo que hace que el fallo de CI senale la historia correcta.

    (El sidecar `active-requests-api` tiene su propio env y ahi NO debe estar:
    ponerlo alli no cambia ninguna ruta y siembra la duda de donde vive.)"""
    dep = _deployment("litellm")
    main = {e["name"]: e.get("value")
            for e in _container(dep, "litellm")["env"]}
    assert main.get("LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES") == "true"
    sidecar = {e["name"] for e in _container(dep, "active-requests-api")["env"]}
    assert "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES" not in sidecar


# ── Grupo 7 de SC-326: vision (VERDE) ─────────────────────────────────────────


def test_el_residente_declara_vision():
    """SC-326 grupo 7: un PNG 16x16 rojo de verdad lo describio "completamente
    roja" — el residente VE. `supports_vision: true` es lo que hace que una
    peticion con imagen se quede en el residente en vez de desviarse (el hook de
    capacidad y OpenClaw/OpenChamber leen ese booleano). El contrato de vision
    (test_local_backends_vision_contract.py) ya lo ancla por su propio motivo;
    aqui entra porque SC-326 lo midio como capability Anthropic de punta a punta
    (bloque `image` base64 de la spec /v1/messages -> data URL OpenAI -> modelo).
    Si el flag miente, el verde de SC-326 se cae y el doc queda describiendo una
    capacidad que el manifiesto niega."""
    info = _entrada(RESIDENTE).get("model_info") or {}
    assert info.get("supports_vision") is True


# ── Grupo 7b de SC-326: count_tokens (VERDE con caveat) ───────────────────────


def test_drop_params_sigue_activado():
    """SC-326 grupo 7b: /v1/messages/count_tokens respondio 200 con input_tokens
    — el rojo esperado (#29764, hardcodeo de api.anthropic.com) NO se reproduce.
    Lo que si garantiza el manifiesto para que la superficie Anthropic no 400ee
    con parametros que el residente no conoce es `drop_params: true`: con el
    apagado, cualquier param extra del cliente Anthropic (thinking, tool_choice
    dict, web_search) puede volver 400 segun el backend. Es la red global que
    sostiene que los casos verdes se sostengan con clientes Anthropic reales."""
    assert _config()["litellm_settings"].get("drop_params") is True


# ── WebSearch (PR #66, verde con caveat de streaming) ─────────────────────────


def test_el_callback_de_websearch_interception_sigue_registrado():
    """PR #66 (mergeado 07-09, verificado en vivo): WebSearch de Claude Code
    contra residentes locales. La server tool nativa `web_search_20250305` la
    ejecuta el callback `websearch_interception`. Si el callback sale de la
    lista, Claude Code vuelve a recibir 400 "When using `tool_choice`, `tools`
    must be set" (el sintoma original que motivo el PR)."""
    callbacks = _config()["litellm_settings"]["callbacks"]
    assert "websearch_interception" in callbacks


def test_websearch_habilita_el_provider_openai_y_el_search_tool_local():
    """SC-326/PR #66: dos piezas mas, ambas medidas, sin las cuales el callback
    es decoracion. (a) `enabled_providers` debe nombrar `openai` — el default de
    upstream es solo ["bedrock"] y TODOS nuestros residentes son `openai/...`,
    asi que sin el nombre explicito la interceptacion jamas dispara. (b) El
    `search_tool_name` referenciado tiene que existir en `search_tools` y apuntar
    al SearXNG del ns chat (GET api_base/search?q=..&format=json, formato json
    verificado 07-09). Renombrar uno u otro sin el otro = busqueda muerta."""
    settings = _config()["litellm_settings"]
    params = settings.get("websearch_interception_params") or {}
    assert "openai" in params.get("enabled_providers", [])
    nombre = params.get("search_tool_name")
    tools = {t["search_tool_name"]: t for t in _config()["search_tools"]}
    assert nombre in tools, f"el search_tool_name {nombre!r} no existe en search_tools"
    st = tools[nombre]["litellm_params"]
    assert st["search_provider"] == "searxng"
    assert "searxng.chat.svc.cluster.local" in st["api_base"]


# ── Los rojos: no se congelan, se documentan (y aqui se dice por que) ─────────


def test_los_rojos_de_sc_326_no_tienen_fix_por_config_en_v1960():
    """SC-327 (investigacion de config): los dos rojos — `tool_choice any`
    (stop_reason tool_use sin bloque tool_use, sin issue upstream conocido) y
    `thinking enabled` sin bloque thinking (#29518) — NO tienen via por config
    en v1.96.0. La evidencia de codigo (sdist oficial litellm-1.96.0) esta en
    doc/anthropic-compat.md. Este test no aserta un knob (no hay): aserta el
    VEREDICTO, para que si alguien "arregla" el rojo anadiendo un knob a mano,
    pase por aqui y actualice el doc y el semaforo en el mismo commit."""
    settings = _config()["litellm_settings"]
    # No existe ningun setting de traduccion thinking/reasoning en nuestra config
    # — y no debe aparecer uno sin actualizar el doc. Los unicos campos de la
    # familia thinking/reasoning que TENEMOS son los del hook propio.
    for clave in ("enable_thinking_translation", "anthropic_thinking_fallback",
                  "map_reasoning_content_to_thinking"):
        assert clave not in settings, (
            f"{clave!r} no existe en litellm v1.96.0 (investigacion SC-327): "
            "si lo anadiste, actualiza doc/anthropic-compat.md y el semaforo."
        )
