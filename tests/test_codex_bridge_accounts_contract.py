"""Contrato del catalogo ChatGPT/Codex publicado por cuenta en LiteLLM.

Cada ID publico se compone como ``cuenta/slug``. El prefijo selecciona de forma
inequivoca el bridge, mientras OpenAI recibe siempre el slug real sin prefijo.
"""

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
    "e-dani": COMMON_MODELS | {"gpt-daybreak-blue-latest"},
}

# Lo que LiteLLM PUBLICA con prefijo de cuenta, que ya no es el catalogo entero.
# Se comprueba por IGUALDAD, asi que todo lo que no este aqui es un alias de mas.
# Dos divergencias, ambas del 2026-08-13 y ambas deliberadas:
#
#   - `gpt-5.6-sol-wm` no se publica: el catalogo lo describe como "Work Mode
#     routing alias for GPT-5.6 Sol". Los mismos pesos que `gpt-5.6-sol`; lo que
#     hace distinto a Work (prompt, conectores, formato) vive en el cliente de
#     ChatGPT y no viaja por `/responses`, donde el puente pone los suyos.
#   - `gpt-daybreak-blue-latest` solo esta en el catalogo de e-dani.
#
# CLOUDBLUE SE REPUBLICA (2026-08-13, decision del operador). Estuvo retirado unas
# horas ese mismo dia con el motivo "sus 4 keys estan bloqueadas a proposito", que
# NO aplica a este camino: esas keys son VIRTUALES de LiteLLM y el puente no las
# usa — habla con ChatGPT por su propia sesion OAuth. Comprobado llamando al
# puente directamente: 200 en 1 s. Los alias si pueden funcionar.
PUBLISHED_MODELS = {
    "cloudblue": ACCOUNT_CATALOG["cloudblue"] - {"gpt-5.6-sol-wm"},
    "e-dani": ACCOUNT_CATALOG["e-dani"] - {"gpt-5.6-sol-wm"},
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


def test_accounts_publish_the_live_catalog_with_account_prefixed_ids():
    documents = _documents(MANIFEST)
    config_map = _resource(documents, "ConfigMap", "litellm-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    models = {model["model_name"]: model for model in config["model_list"]}

    services = {
        "cloudblue": "http://codex-bridge.litellm.svc.cluster.local:8080/v1",
        "e-dani": "http://codex-bridge-edani.litellm.svc.cluster.local:8080/v1",
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
    assert not any(name.endswith("-edani") for name in models)


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
    assert set(env["ALLOWED_MODELS"].split(",")) >= ACCOUNT_CATALOG["e-dani"]


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
