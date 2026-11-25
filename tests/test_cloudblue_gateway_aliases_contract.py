"""Los alias de la pasarela LiteLLM de CloudBlue por el puente de mac-cb (DGX-1826).

POR QUE EXISTE ESTE TEST (25-11-2026). Cuatro alias `deepseek-*` del model_list no
salen a un backend de casa ni a Internet: van a la tailnet, a un puente Node en mac-cb
(100.125.62.10:8720) que los reenvia a https://litellm.com.int.zone por la VPN de
CloudBlue, con la virtual key `daniel-openrouter` (DO-32862) como unica credencial.
Lo que hace seguro ese camino vive en el yaml y se pierde en silencio:

- sin `disable_background_health_check`, cada sondeo de fondo es una inferencia DE PAGO
  contra la pasarela de CloudBlue (y con el Mac dormido, ruido de errores);
- sin `num_retries: 0`, un Mac dormido cuesta tres timeouts por peticion;
- sin la entrada del ExternalSecret o sin el env, el alias se publica y contesta 401.

Este archivo congela esas piezas. Contrato: dgx.litellm.model-alias.cloudblue-gateway.v1.
"""
import pathlib

import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
CONTRACTS = pathlib.Path(__file__).resolve().parents[1] / "CONTRACTS.yaml"

ALIASES = {
    "deepseek-v4-flash-0731",
    "deepseek-v4-flash-0731-loc",
    "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-1017",
}
BRIDGE = "http://100.125.62.10:8720/v1"
ENV = "CLOUDBLUE_LITELLM_KEY"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if isinstance(d, dict)]


def _model_list():
    for d in _docs():
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config":
            return yaml.safe_load(d["data"]["config.yaml"])["model_list"]
    raise AssertionError("litellm-config sin config.yaml")


def _entries():
    return {m["model_name"]: m for m in _model_list() if m.get("model_name") in ALIASES}


def test_los_cuatro_alias_van_al_puente_de_mac_cb():
    entries = _entries()
    assert set(entries) == ALIASES
    names = [m["model_name"] for m in _model_list()]
    for alias in ALIASES:
        assert names.count(alias) == 1, alias
        params = entries[alias]["litellm_params"]
        assert params["model"] == f"openai/{alias}"
        assert params["api_base"] == BRIDGE
        assert params["api_key"] == f"os.environ/{ENV}"


def test_sin_sondeo_de_fondo_sin_reintento_y_coste_cero():
    for alias, entry in _entries().items():
        info = entry["model_info"]
        params = entry["litellm_params"]
        assert info["disable_background_health_check"] is True, alias
        assert params["num_retries"] == 0, alias
        assert info["input_cost_per_token"] == 0 and info["output_cost_per_token"] == 0, alias


def test_la_key_llega_al_contenedor_por_el_externalsecret():
    docs = _docs()
    es = next(d for d in docs if d.get("kind") == "ExternalSecret"
              and d["metadata"]["name"] == "litellm-secrets")
    data = {e["secretKey"]: e["remoteRef"]["key"] for e in es["spec"]["data"]}
    assert data[ENV] == f"litellm/{ENV}"

    dep = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm")
    container = next(c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "litellm")
    env = {e["name"]: e for e in container.get("env", [])}
    ref = env[ENV]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == "litellm-secrets" and ref["key"] == ENV


# El ancla literal del contrato (CONTRACTS.yaml la busca en cada fichero de `files`).
ANCHOR = "api_base: http://100.125.62.10:8720/v1"


def test_el_ancla_sigue_en_el_manifest():
    assert MANIFEST.read_text().count(ANCHOR) == len(ALIASES)


def test_el_contrato_esta_declarado():
    contracts = yaml.safe_load(CONTRACTS.read_text())["contracts"]
    c = next(c for c in contracts if c["id"] == "dgx.litellm.model-alias.cloudblue-gateway.v1")
    assert c["status"] == "active"
    assert c["value"] in ALIASES
    assert c["anchor"] == ANCHOR
