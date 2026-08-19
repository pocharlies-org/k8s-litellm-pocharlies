"""Reasoning metadata published for the live DeepSeek backend.

DeepSeek's compatibility aliases (tooling/agent/high/max) remain available for
clients that can only choose a model name. OpenCode and OpenChamber can send a
reasoning effort, so the direct model must advertise its real tiers and let the
client render one model with variants instead of four apparent checkpoints.
"""

import ast
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _sync_code() -> str:
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for content in (doc.get("data") or {}).values():
            if "BACKENDS = (" in content and "managed_model_contract" in content:
                return content
    raise AssertionError("no encuentro el codigo del backend-sync en el manifiesto")


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

    El discriminador es el `api_base`: los alias de nube (gpt-5.6) declaran
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


def test_los_backends_del_sync_declaran_lo_mismo_que_el_config():
    """El controlador es la otra fuente de model_info: registra por /model/new los
    alias que no estan en el config estatico (los `dense*` del perfil creative).
    Si su tabla se queda con `low`, la mentira vuelve por ahi."""
    honrados = set(_client_effort_tiers())
    tree = ast.parse(_sync_code())
    vistos = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "BACKENDS" for t in node.targets):
            continue
        for element in node.value.elts:
            entry = {}
            for key, value in zip(element.keys, element.values):
                if not isinstance(key, ast.Constant):
                    continue
                try:
                    entry[key.value] = ast.literal_eval(value)
                except ValueError:
                    entry[key.value] = "<dinamico>"
            efforts = entry.get("supported_reasoning_efforts")
            if not efforts:
                continue
            vistos += 1
            sobran = set(efforts) - honrados
            assert not sobran, f"{entry.get('name')} declara {sorted(sobran)}"
    assert vistos, "no he encontrado backends con efforts declarados"


def _deepseek_backend() -> dict:
    tree = ast.parse(_sync_code())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(target, "id", "") == "BACKENDS" for target in node.targets):
            continue
        for element in node.value.elts:
            entry = {}
            for key, value in zip(element.keys, element.values):
                if not isinstance(key, ast.Constant):
                    continue
                try:
                    entry[key.value] = ast.literal_eval(value)
                except ValueError:
                    entry[key.value] = "<dinamico>"
            if entry.get("name") == "deepseek-v4-flash-tp2":
                return entry
    raise AssertionError("no encuentro deepseek-v4-flash-tp2 en BACKENDS")


def test_deepseek_publica_sus_tiers_reales():
    backend = _deepseek_backend()
    assert backend["supports_reasoning"] is True
    # 2026-08-19: `low` FUERA, y no es cosmetica. El hook no traduce `low`
    # (CLIENT_EFFORT_TIERS: un effort ambiente de un cliente agente no es una
    # orden), asi que declararlo dejaba a OpenClaw parado en un nivel que el
    # servidor descarta -- creyendo pensar mientras `tooling` corria en
    # thinking_mode "chat" y el monologo de DeepSeek salia por el canal visible.
    assert backend["supported_reasoning_efforts"] == (
        "none",
        "high",
        "max",
    )


def test_el_reconciler_refresca_reasoning_y_efforts():
    code = _sync_code()
    desired_start = code.index("def desired_deployments")
    desired = code[desired_start:code.index("def current_models_by_id")]
    assert '"supports_reasoning": bool(backend.get("supports_reasoning", False))' in desired
    assert 'backend.get("supported_reasoning_efforts", ())' in desired

    contract_start = code.index("def managed_model_contract")
    contract = code[contract_start:code.index("def add_model")]
    assert '"supports_reasoning": bool(info.get("supports_reasoning", False))' in contract
    assert 'info.get("supported_reasoning_efforts") or ()' in contract
