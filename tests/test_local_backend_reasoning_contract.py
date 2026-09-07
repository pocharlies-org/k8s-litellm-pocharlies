"""Reasoning metadata publicada para el residente llm-tp vivo.

Los alias de capacidad (tooling/high/max) siguen disponibles para clientes que
solo saben elegir un nombre de modelo. OpenCode y OpenChamber si pueden mandar un
reasoning effort, asi que el alias directo tiene que anunciar sus tiers REALES y
dejar que el cliente pinte un modelo con variantes en vez de cuatro checkpoints
aparentes.

07-09-2026: este fichero leia el `BACKENDS` del ConfigMap de
`litellm-dgx-backend-sync` como segunda fuente de model_info. Ese controlador se
borro del repo — llevaba muerto desde el 18-08 —, asi que la unica fuente es el
`model_list` estatico y aqui no hay ya nada que reconciliar entre dos sitios.
"""

import ast
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _configmap_value(marker: str) -> str:
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for content in (doc.get("data") or {}).values():
            if marker in content:
                return content
    raise AssertionError(f"no encuentro un ConfigMap con {marker!r}")


def _client_effort_tiers() -> dict:
    """La tabla del hook, leida del manifiesto (no se duplica a mano aqui)."""
    tree = ast.parse(_configmap_value("CLIENT_EFFORT_TIERS = {"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(t, "id", "") == "CLIENT_EFFORT_TIERS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("no encuentro CLIENT_EFFORT_TIERS")


def _local_aliases_with_efforts() -> list[tuple[str, list[str]]]:
    """Alias servidos DENTRO del cluster que declaran niveles de esfuerzo.

    El discriminador es el `api_base`: los alias de nube declaran
    low/medium/xhigh/ultra y hacen bien -- su effort viaja al upstream, que si los
    honra. Los locales pasan por _apply_thinking_tier, y ahi manda la tabla.
    """
    config = yaml.safe_load(_configmap_value("model_list:"))
    out = []
    for entry in config["model_list"]:
        params = entry.get("litellm_params") or {}
        info = entry.get("model_info") or {}
        efforts = info.get("supported_reasoning_efforts")
        if not efforts:
            continue
        if ".llm.svc.cluster.local" not in str(params.get("api_base") or ""):
            continue
        out.append((entry["model_name"], list(efforts)))
    return out


def test_ningun_alias_local_anuncia_un_effort_que_el_hook_descarta():
    """El menu publicado y la tabla del hook son UNA sola verdad.

    Esta es la mitad que faltaba del contrato: `test_client_effort_contract` fija
    que `low`/`medium` no se traducen, pero /model/info los seguia anunciando. Un
    nivel anunciado y no honrado no es cosmetico -- OpenClaw lo guarda como nivel
    de sesion, cree estar pensando, y el alias corre sin canal de razonamiento
    mientras el modelo deliberaba en el canal visible (56 fugas en 421 mensajes
    de `tooling`, medido el 19-08-2026).
    """
    honrados = set(_client_effort_tiers())
    locales = _local_aliases_with_efforts()
    assert locales, "no he encontrado ningun alias local con efforts declarados"
    for alias, efforts in locales:
        sobran = set(efforts) - honrados
        assert not sobran, f"{alias} anuncia {sorted(sobran)}, que el hook descarta"


def _llm_tp_backend() -> dict:
    config = yaml.safe_load(_configmap_value("model_list:"))
    for entry in config["model_list"]:
        if entry["model_name"] == "qwen38-flash-next":
            return entry.get("model_info") or {}
    raise AssertionError("no encuentro qwen38-flash-next en el model_list")


def test_el_residente_llm_tp_publica_sus_tiers_reales():
    """01-09-2026: hereda el ancla de DeepSeek-V4-Flash al retirarse este.

    La lista honesta es la misma y por el mismo motivo: son los tiers que el
    HOOK honra (CLIENT_EFFORT_TIERS), no los que acepta el servidor.
    """
    backend = _llm_tp_backend()
    assert backend["supports_reasoning"] is True
    # 2026-08-19: `low` FUERA, y no era cosmetica — el hook no lo traducia y
    # anunciarlo dejaba a OpenClaw creyendo pensar. 05-09-2026 (SC-203): ENTRA
    # `low` y entra `medium`, y por el mismo criterio de siempre: el hook YA
    # los traduce (CLIENT_EFFORT_TIERS/THINKING_KWARGS) y el default efectivo
    # del residente es `low`. Lo que se midio el 01-09 ("low/medium dan 0
    # chars") era el motor sin --reasoning-parser (D1/SC-204: lo lleva) y con
    # el default del hook en off; medido hoy via proxy, ambos dan reasoning
    # real no vacio. `xhigh` sigue (=max) y `high`/`max` se mantienen como
    # alias deprecados del vocabulario del cliente.
    assert list(backend["supported_reasoning_efforts"]) == [
        "none",
        "low",
        "medium",
        "high",
        "max",
        "xhigh",
    ]


def test_el_residente_llm_tp_no_anuncia_niveles_que_el_hook_no_traduce():
    """05-09-2026 (SC-203): este test se llamaba `..._no_anuncia_los_inertes` y
    prohibia `low`/`medium`. Su premisa ("inertes en nuestro motor") era falsa
    — se midio sin --reasoning-parser y con el razonamiento apagado por el
    propio hook — y hoy esta refutada por medicion via proxy. El contrato que
    queda es el que siempre fue el verdadero: lo que se anuncia tiene que
    traducirlo el hook. `ultra`/`high`-crudo no estan en la tabla del hook,
    luego no se anuncian."""
    backend = _llm_tp_backend()
    efforts = set(backend["supported_reasoning_efforts"])
    honrados = set(_client_effort_tiers())
    assert efforts <= honrados, f"anuncia {sorted(efforts - honrados)}"
    # Y el menu oficial esta completo: none/low/medium/xhigh.
    assert {"none", "low", "medium", "xhigh"} <= efforts
