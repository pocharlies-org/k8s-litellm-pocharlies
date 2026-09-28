"""Contrato del reintento acotado del 400 de `function.arguments` en Alibaba.

POR QUE EXISTE (27-09-2026)
---------------------------
23 peticiones en 12 h contra `alibaba-q38-flash` murieron con

  litellm.BadRequestError: ... The "function.arguments" parameter of the code
  model must be in JSON format.

y la causa NO estaba en el request. Tres mediciones lo descartaron (ver
tests/test_tool_call_arguments_json_contract.py y el comentario del manifest):
los 2424 `input` que OpenCode almacena son todos dict, los 5252 tool_calls
trazados en Langfuse llevan arguments de objeto valido, y el fallo es
TRANSITORIO -- en ses_f342de2bdffe falla 19:07:28 y la MISMA sesion sirve bien
19:07:35 con el mismo historial mas un mensaje. Lo que cuadra con eso es que
Model Studio invalide los arguments que genera su propio modelo: un fallo de
muestreo, no del request.

El Router ya reintentaba 1 vez (el `num_retries` global; en el metadata de esos
fallos se lee `max_retries: 1`), o sea que cayo dos veces seguidas. Este fichero
fija el presupuesto subido PARA ESE GRUPO y la condicion de que siga sin tocar
los demas.

La politica se resuelve por modelo en get_retry_from_policy.py:51 y el proxy
reenvia `model_group_retry_policy` al Router porque es argumento valido de
Router.__init__ (verificado en la fuente pineada: proxy_server.py:5722-5732
filtra por get_valid_args() y, si la clave no lo fuera, avisa e la ignora).
"""
import pathlib

import pytest
import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

GRUPO = "alibaba-q38-flash"


@pytest.fixture(scope="module")
def router_settings():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    cfg = yaml.safe_load(cm["data"]["config.yaml"])
    return cfg["router_settings"]


def test_el_grupo_de_alibaba_tiene_presupuesto_de_reintentos_400(router_settings):
    """Sin esto, un 400 transitorio de Model Studio revienta el turno del usuario."""
    policy = router_settings.get("model_group_retry_policy") or {}
    assert GRUPO in policy, (
        f"`{GRUPO}` sin politica de reintentos: el 400 de function.arguments "
        "vuelve a ser terminal"
    )
    reintentos = policy[GRUPO].get("BadRequestErrorRetries")
    # 2 = tres generaciones nuevas por turno. El 1 global ya se midio insuficiente
    # (max_retries: 1 en los fallos del 27-09).
    assert isinstance(reintentos, int) and reintentos >= 2, (
        f"BadRequestErrorRetries={reintentos!r}: con 1 se cayo dos veces el 27-09"
    )


def test_no_es_una_politica_global(router_settings):
    """El alcance es el grupo. Un retry_policy global reintentaria el 400 AJENO.

    Un 400 de verdad (parametro invalido del cliente, contexto desbordado) no
    tiene porque repetirse tres veces contra el residente: ahi solo se paga
    latencia y coste. El `num_retries` global se queda como estaba.
    """
    assert not router_settings.get("retry_policy"), (
        "hay retry_policy global: el presupuesto de 400 tiene que vivir SOLO en "
        "model_group_retry_policy"
    )
    assert router_settings.get("num_retries") == 1


def test_la_clave_no_se_replica_en_otros_grupos(router_settings):
    """Lo que no se midio no se toca: solo el grupo que dio los 400."""
    policy = router_settings.get("model_group_retry_policy") or {}
    assert set(policy) == {GRUPO}, f"grupos con politica inesperada: {sorted(policy)}"
