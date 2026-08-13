"""Contrato de las dos cuentas ChatGPT/Codex publicadas por LiteLLM.

Cada cuenta debe ofrecer el mismo catalogo manual: Sol, Terra, Luna y Codex
Spark. La cuenta se selecciona exclusivamente por el Service del bridge; el
slug que recibe OpenAI permanece sin sufijos locales.
"""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
BRIDGES = ROOT / "k8s" / "codex-bridge.yaml"

UPSTREAM_MODELS = {
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.3-codex-spark",
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


def test_both_accounts_publish_sol_terra_luna_and_codex_spark():
    documents = _documents(MANIFEST)
    config_map = _resource(documents, "ConfigMap", "litellm-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    models = {model["model_name"]: model for model in config["model_list"]}

    expected = {
        "cloudblue": {
            model: (
                model,
                "http://codex-bridge.litellm.svc.cluster.local:8080/v1",
            )
            for model in UPSTREAM_MODELS
        },
        "edani": {
            f"{model}-edani": (
                model,
                "http://codex-bridge-edani.litellm.svc.cluster.local:8080/v1",
            )
            for model in UPSTREAM_MODELS
        },
    }

    for account in expected.values():
        for alias, (upstream, api_base) in account.items():
            assert alias in models
            params = models[alias]["litellm_params"]
            assert params["model"] == f"openai/{upstream}"
            assert params["api_base"] == api_base


def test_personal_bridge_is_enabled_with_its_own_secret():
    documents = _documents(BRIDGES)
    deployment = _resource(documents, "Deployment", "codex-bridge-edani")
    assert deployment["spec"]["replicas"] == 1

    pod_spec = deployment["spec"]["template"]["spec"]
    auth_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "auth")
    assert auth_volume["secret"]["secretName"] == "codex-bridge-edani-auth"

    bridge = next(container for container in pod_spec["containers"] if container["name"] == "bridge")
    env = {entry["name"]: entry.get("value") for entry in bridge["env"]}
    assert env["SECRET_NAME"] == "codex-bridge-edani-auth"
    assert set(env["ALLOWED_MODELS"].split(",")) >= UPSTREAM_MODELS
