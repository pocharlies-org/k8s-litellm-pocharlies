"""Ningun backend local de razonamiento puede volver al bucle de health checks.

POR QUE EXISTE (2026-09-02, STA-9)
----------------------------------
`/health` decia `healthy_count: 0` MIENTRAS una inferencia real respondia `391` en
5 s. El panel traduce eso en un semaforo, y el humano lo vio verde con el numero
mal y rojo con el servicio bueno. La causa no es el watchdog: es el bucle de fondo
de LiteLLM (`background_health_checks: true`), que sondea con `max_tokens` 16 --
el default de upstream, `health_check.py::_resolve_health_check_max_tokens`. Aqui
todos los residentes son de razonamiento: el pensamiento se come los 16 tokens y
devuelve `content` VACIO con HTTP 200, que el bucle apunta como unhealthy.

El mismo bug ya estaba documentado para el test del cron del watchdog (ver el
comentario de `max_tokens` en `k8s/litellm-watchdog-cron.yaml`), pero nadie lo
aplico al health-check interno. Y el coste: 2 replicas x cada alias x 96 ciclos
por dia de inferencias que no informan de nada.

Este contrato es la parte que se puede verificar sin ejecutar LiteLLM: que los
locales siguen callados. El bucle deduplica por `model_info.id`, no por `api_base`
(ver el comentario de `bge-m3-embedding`), asi que callar solo uno de los alias que
apuntan al MISMO motor no calla nada: hay que callarlos todos. Lo que se puede
verificar sin ejecutar LiteLLM es que NINGUN alias de un api_base local tiene el
flag a False -- por eso la comprobacion va por api_base compartido, no por alias.

Lo que NO comprueba, y por eso no se puede mechanicalizar aqui: que la sonda de 16
tokens sea inadecuada para un modelo de razonamiento. Eso es upstream y se ve solo
ejecutando; la unica senal honesta del estado real sigue siendo la inferencia del
watchdog.
"""
import pathlib

import pytest
import yaml


MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# Unico modo con el bug de los 16 tokens: una generacion de texto que puede
# devolver `content` vacio con HTTP 200 porque el razonamiento se comio el limite.
# `embedding` y `rerank` se sondean de verdad y siguen activos a proposito: no
# piensan, y su cobertura interesa. `audio_transcription` y `audio_speech` tampoco
# son chat -- y ademas sus gemelos (whisper-1/tts-1) son alias OpenAI-SDK sobre el
# MISMO backend, que es justo lo que el bucle deduplica mal; callarlos es una
# decision aparte, no la que toma este contrato.
INFERENCIA_CHAT = {"chat"}


def _model_list():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    return yaml.safe_load(cm["data"]["config.yaml"])["model_list"]


def _is_local(api_base):
    return "cluster.local" in str(api_base or "")


LOCAL_CHAT = [
    m for m in _model_list()
    if _is_local((m.get("litellm_params") or {}).get("api_base"))
    and (m.get("model_info") or {}).get("mode") in INFERENCIA_CHAT
]


@pytest.mark.parametrize("entry", LOCAL_CHAT, ids=lambda m: m["model_name"])
def test_los_alias_de_un_motor_local_no_sondean_por_bucle(entry):
    """O todos los alias de un motor callados, o el bucle sigue gastando e mintiendo.

    Un alias nuevo de un backend local que se declare SIN el flag falla aqui, que
    es donde se ve, y no en el panel con el semaforo mentiendo.
    """
    api_base = entry["litellm_params"]["api_base"]
    mode = (entry.get("model_info") or {}).get("mode")
    siblings = [
        m["model_name"] for m in LOCAL_CHAT
        if m["litellm_params"]["api_base"] == api_base
    ]
    callados = [
        m["model_name"] for m in LOCAL_CHAT
        if m["litellm_params"]["api_base"] == api_base
        and (m.get("model_info") or {}).get("disable_background_health_check") is True
    ]
    assert len(siblings) == len(callados), (
        f"{entry['model_name']} (mode={mode}) apunta a {api_base} y el bucle de "
        f"fondo deduplica por model_info.id, no por api_base: los alias del mismo "
        f"motor se callan TODOS o ninguno. Callados: {callados}; del motor: {siblings}."
    )


def test_el_alias_de_capacidad_comparte_motor_con_su_residente():
    """`tooling`/`tooling-uncensored` apuntan al Service de pool del residente vivo.

    Si alguien mueve el alias de capacidad a otro api_base, deja de ser un espejo
    del residente y este contrato deja de decir lo que cree decir: mejor fallar
    aqui que descubrirlo mirando un semaforo.
    """
    by_name = {m["model_name"]: m for m in _model_list()}
    pool = by_name["tooling"]["litellm_params"]["api_base"]
    assert "tooling.llm.svc.cluster.local" in pool
    assert by_name["tooling-uncensored"]["litellm_params"]["api_base"] == pool


def test_el_bucle_de_fondo_sigue_activo_globalmente():
    """Se callan los locales, no el bucle.

    El bucle sigue siendo la unica sena automatica de los upstream de pago
    (openrouter, codex bridge) cuando cambian de estado. Apagar
    `background_health_checks` entero dejaria el panel sin ninguna senal continua.
    """
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    general = yaml.safe_load(cm["data"]["config.yaml"])["general_settings"]
    assert general.get("background_health_checks") is True
    assert general.get("health_check_interval") == 900
