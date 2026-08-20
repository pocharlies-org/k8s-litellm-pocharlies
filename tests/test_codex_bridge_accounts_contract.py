"""Contrato del catalogo ChatGPT/Codex publicado por cuenta en LiteLLM.

Cada ID publicado se compone como ``cloudblue/slug``. El bridge e-dani
desactivado no debe dejar declaraciones muertas ``e-dani/*`` ni ``*-edani``.
"""

import ast
import types
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
BRIDGES = ROOT / "k8s" / "codex-bridge.yaml"

# Lo que la CUENTA sirve aguas arriba, segun el catalogo autoritativo
# (GET /backend-api/codex/models). Gobierna el ALLOWED_MODELS de cada puente y se
# comprueba como SUPERCONJUNTO: un puente puede aceptar mas de lo que LiteLLM
# publique, pero nunca menos, porque el slug que no esta en ALLOWED_MODELS cae al
# UPSTREAM_MODEL y contesta 200 con otro modelo, sin error ninguno.
COMMON_MODELS = {
    "gpt-5.6-sol",
    "gpt-5.6-sol-wm",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.3-codex-spark",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "codex-auto-review",
}

ACCOUNT_CATALOG = {
    "cloudblue": COMMON_MODELS,
    "e-dani": COMMON_MODELS,
}

# Lo que LiteLLM PUBLICA con prefijo de cuenta, que ya no es el catalogo entero.
# Se comprueba por IGUALDAD, asi que todo lo que no este aqui es un alias de mas.
# Dos divergencias, ambas del 2026-08-13 y ambas deliberadas:
#
#   - `gpt-5.6-sol-wm` no se publica: el catalogo lo describe como "Work Mode
#     routing alias for GPT-5.6 Sol". Los mismos pesos que `gpt-5.6-sol`; lo que
#     hace distinto a Work (prompt, conectores, formato) vive en el cliente de
#     ChatGPT y no viaja por `/responses`, donde el puente pone los suyos.
#   - `gpt-daybreak-blue-latest` no se publica en ninguna cuenta (2026-08-13,
#     decision del operador). Solo existia en el catalogo de e-dani.
#
# CLOUDBLUE SE REPUBLICA (2026-08-13, decision del operador). Estuvo retirado unas
# horas ese mismo dia con el motivo "sus 4 keys estan bloqueadas a proposito", que
# NO aplica a este camino: esas keys son VIRTUALES de LiteLLM y el puente no las
# usa — habla con ChatGPT por su propia sesion OAuth. Comprobado llamando al
# puente directamente: 200 en 1 s. Los alias si pueden funcionar.
PUBLISHED_MODELS = {
    "cloudblue": ACCOUNT_CATALOG["cloudblue"] - {"gpt-5.6-sol-wm"},
}

REASONING_EFFORTS = {
    "gpt-5.6-sol": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-5.6-terra": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"],
}


def _documents(path):
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def _resource(documents, kind, name):
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_only_enabled_account_models_are_published():
    documents = _documents(MANIFEST)
    config_map = _resource(documents, "ConfigMap", "litellm-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    models = {model["model_name"]: model for model in config["model_list"]}

    services = {
        "cloudblue": "http://codex-bridge.litellm.svc.cluster.local:8080/v1",
    }
    expected = {
        account: {
            f"{account}/{model}": (model, services[account])
            for model in account_models
        }
        for account, account_models in PUBLISHED_MODELS.items()
    }

    for account in expected.values():
        for alias, (upstream, api_base) in account.items():
            assert alias in models
            params = models[alias]["litellm_params"]
            assert params["model"] == f"openai/{upstream}"
            assert params["api_base"] == api_base

    public_names = {
        name for name in models
        if name.startswith("cloudblue/") or name.startswith("e-dani/")
    }
    assert public_names == set().union(*[set(account) for account in expected.values()])

    assert not {name for name in models if name.startswith("e-dani/")}
    assert not {name for name in models if name.endswith("-edani")}


def test_desktop_cloudblue_aliases_keep_the_prefixed_fallback_network():
    """LiteLLM keys fallbacks by exact model_group, not by equivalent deployment.

    Codex Desktop sends the bare model id. If only the ``cloudblue/<id>`` source
    is registered, the bare alias still needs an explicit edge to that deployment.
    """
    documents = _documents(MANIFEST)
    config_map = _resource(documents, "ConfigMap", "litellm-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    entries = config["router_settings"]["fallbacks"]
    fallbacks = {
        source: destinations
        for entry in entries
        for source, destinations in entry.items()
    }

    for model in PUBLISHED_MODELS["cloudblue"]:
        assert fallbacks[model] == [f"cloudblue/{model}"]
        assert f"cloudblue/{model}" not in fallbacks


def test_personal_bridge_remains_disabled_with_its_own_secret():
    documents = _documents(BRIDGES)
    deployment = _resource(documents, "Deployment", "codex-bridge-edani")
    assert deployment["spec"]["replicas"] == 0

    pod_spec = deployment["spec"]["template"]["spec"]
    auth_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "auth")
    assert auth_volume["secret"]["secretName"] == "codex-bridge-edani-auth"

    bridge = next(container for container in pod_spec["containers"] if container["name"] == "bridge")
    env = {entry["name"]: entry.get("value") for entry in bridge["env"]}
    assert env["SECRET_NAME"] == "codex-bridge-edani-auth"
    assert set(env["ALLOWED_MODELS"].split(",")) >= ACCOUNT_CATALOG["e-dani"]


def test_gpt_56_models_publish_their_real_reasoning_efforts():
    documents = _documents(MANIFEST)
    config_map = _resource(documents, "ConfigMap", "litellm-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    models = {model["model_name"]: model for model in config["model_list"]}

    for slug, efforts in REASONING_EFFORTS.items():
        names = (f"cloudblue/{slug}", slug)
        for name in names:
            info = models[name]["model_info"]
            assert info["supports_reasoning"] is True
            assert info["supports_low_reasoning_effort"] is True
            assert info["supports_xhigh_reasoning_effort"] is True
            assert info["supports_max_reasoning_effort"] is True
            assert info["supported_reasoning_efforts"] == efforts


def _reasoning_merge_functions():
    documents = _documents(MANIFEST)
    config_map = _resource(documents, "ConfigMap", "litellm-config")
    tree = ast.parse(config_map["data"]["litellm_strip_params.py"])
    wanted = {
        "_reasoning_effort_capabilities",
        "_merge_catalog_reasoning_capabilities",
    }
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {function.name for function in functions} == wanted
    module = types.ModuleType("model_info_reasoning_contract")
    exec(
        compile(ast.Module(body=functions, type_ignores=[]), "<hook>", "exec"),
        module.__dict__,
    )
    return module._merge_catalog_reasoning_capabilities


def test_model_info_preserves_catalog_reasoning_capabilities_generically():
    merge = _reasoning_merge_functions()
    response = {
        "data": [{
            "model_name": "future/model",
            "model_info": {
                "supports_xhigh_reasoning_effort": False,
                "max_input_tokens": 123,
            },
        }]
    }
    catalog = [{
        "model_name": "future/model",
        "model_info": {
            "supported_reasoning_efforts": ["low", "ultra"],
            "supports_xhigh_reasoning_effort": True,
            "supports_ultra_reasoning_effort": True,
            "max_input_tokens": 999,
        },
    }]

    assert merge(response, catalog) is response
    info = response["data"][0]["model_info"]
    assert info["supported_reasoning_efforts"] == ["low", "ultra"]
    assert info["supports_xhigh_reasoning_effort"] is True
    assert info["supports_ultra_reasoning_effort"] is True
    assert info["max_input_tokens"] == 123


def test_model_info_capability_merge_does_not_guess_from_model_names():
    merge = _reasoning_merge_functions()
    response = {"data": [{"model_name": "gpt-999-ultra", "model_info": {}}]}
    merge(response, [{"model_name": "gpt-999-ultra", "model_info": {}}])
    assert response["data"][0]["model_info"] == {}

    hook = _resource(_documents(MANIFEST), "ConfigMap", "litellm-config")["data"][
        "litellm_strip_params.py"
    ]
    assert 'MODEL_INFO_PATHS = frozenset({"/model/info", "/v1/model/info"})' in hook


def test_cloudblue_bridge_allows_its_complete_catalog():
    documents = _documents(BRIDGES)
    deployment = _resource(documents, "Deployment", "codex-bridge")
    bridge = next(
        container
        for container in deployment["spec"]["template"]["spec"]["containers"]
        if container["name"] == "bridge"
    )
    env = {entry["name"]: entry.get("value") for entry in bridge["env"]}
    assert set(env["ALLOWED_MODELS"].split(",")) >= ACCOUNT_CATALOG["cloudblue"]
