"""26-11-2026 (SC-6751, causa de SC-6675): un prompt flagged de la suscripcion ChatGPT no aparca.

El plan ChatGPT (OAuth, sin API key) devuelve el rechazo AL PROMPT como `ContentPolicyViolationError`
con `status_code` 200 (la respuesta viene en el cuerpo, no como 4XX). En el fuente pineado (160cffc07b)
`cooldown_handlers._is_cooldown_required` solo salva las 4XX (400 -> False); con 200 cae en
"cool down for all other errors" -> True, y como cada alias `chatgpt/*` de chat es una UNICA deployment
sin fallback, el `allowed_fails: 3` + `cooldown_time: 300` del router aparca 300 s a TODOS los
consumidores por un prompt flagged de uno (medido 545 «No deployments available ...gpt-6-luna» en 6 h).

El remedio NO es el ancla `&resident_never_cools` (su `cooldown_time: 0` anularia tambien el cooldown
real del motor). Cada alias de chat lleva el ancla `&chatgpt_prompt_error_never_cools`: SOLO
`ContentPolicyViolationErrorAllowedFails` a un techo inalcanzable. La clave del contador lleva sufijo por
tipo de excepcion, asi que un prompt flagged no sube el contador base que usa el motor, y un
429/5xx/timeout (sin campo nombrado) sigue cayendo al `allowed_fails: 3` + `cooldown_time: 300` del
router: cooldown real mantenido. Lo fija ESTE test.
"""
import pytest
import yaml

from test_aurora_fallback_policy_contract import MANIFEST

TECHO_MINIMO = 10**9
NO_CHAT_MODES = ("image_generation", "realtime")


def _config():
    return next(
        yaml.safe_load(doc["data"]["config.yaml"])
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc
        and doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )


def _chatgpt(mode_chat: bool):
    sel = []
    for m in _config()["model_list"]:
        lp = m.get("litellm_params") or {}
        mi = m.get("model_info") or {}
        if not lp.get("model", "").startswith("chatgpt/"):
            continue
        es_chat = mi.get("mode") not in NO_CHAT_MODES
        if es_chat == mode_chat:
            sel.append(m)
    return sel


CHAT = _chatgpt(True)
NO_CHAT = _chatgpt(False)


def test_hay_alias_de_chat_que_cubrir():
    nombres = {m["model_name"] for m in CHAT}
    assert "gpt-6-luna" in nombres, "gpt-6-luna (el medido en SC-6675) debe estar en la familia cubierta"
    assert len(CHAT) >= 10, f"se esperaban los alias chat de la suscripcion, hay {len(CHAT)}"


@pytest.mark.parametrize("entrada", CHAT, ids=lambda m: m["model_name"])
def test_el_chat_de_suscripcion_no_aparca_por_prompt(entrada):
    mi = entrada.get("model_info") or {}
    lp = entrada.get("litellm_params") or {}
    politica = mi.get("allowed_fails_policy") or {}
    techo = politica.get("ContentPolicyViolationErrorAllowedFails")
    assert techo is not None and techo >= TECHO_MINIMO, (
        f"{entrada['model_name']}: sin ContentPolicyViolationErrorAllowedFails alto ({techo})"
    )
    # cooldown_time NO fijado: hereda el cooldown real del router (300) para 429/5xx/timeout.
    assert "cooldown_time" not in mi, (
        f"{entrada['model_name']}: cooldown_time en model_info anularia el cooldown real del motor"
    )
    assert "cooldown_time" not in lp, (
        f"{entrada['model_name']}: cooldown_time en litellm_params se copia a la llamada al proveedor"
    )


@pytest.mark.parametrize("entrada", NO_CHAT, ids=lambda m: m["model_name"])
def test_la_exencion_no_se_filtra_a_imagen_o_realtime(entrada):
    # La politica es solo para chat (el sintoma medido); imagen/realtime siguen su camino.
    mi = entrada.get("model_info") or {}
    assert "ContentPolicyViolationErrorAllowedFails" not in (mi.get("allowed_fails_policy") or {}), (
        f"{entrada['model_name']}: modo {mi.get('mode')} no debe llevar la exencion de prompt"
    )
