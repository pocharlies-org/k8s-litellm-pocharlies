"""Contrato del retiro del flotante `alibaba-dsv41-fl` (22-10-2026).

El upstream `deepseek-v4.1-flash` sin sufijo (flotante) se retira: todo va al pin
`deepseek-v4.1-flash-1017`, publicado como `alibaba-ds41f1017` (DGX-1190). El nombre
viejo y el largo siguen contestando como ALIAS del pin (`model_group_alias`), para
que quien aun los pida no reciba un 404.

Que vigila:

  1. Ningun deployment pide el upstream flotante (`openai/deepseek-v4.1-flash` sin
     sufijo) y `alibaba-dsv41-fl` ya no es un grupo del model_list.
  2. `alibaba-dsv41-fl` y `alibaba-deepseek-v41-flash` apuntan DIRECTO al pin (un
     solo salto: el alias no se encadena con otro alias).
  3. El pin conserva sus dos deployments (-k1/-k2) sobre el upstream `-1017`.
"""
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

PIN = "alibaba-ds41f1017"
FLOTANTE = "openai/deepseek-v4.1-flash"
ALIAS_AL_PIN = ("alibaba-dsv41-fl", "alibaba-deepseek-v41-flash")


@pytest.fixture(scope="module")
def config():
    cm = next(
        d for d in yaml.safe_load_all(MANIFEST.read_text())
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    return yaml.safe_load(cm["data"]["config.yaml"])


def test_ningun_deployment_pide_el_upstream_flotante(config):
    flotantes = sorted(
        e["model_info"]["id"] for e in config["model_list"]
        if e["litellm_params"].get("model") == FLOTANTE
    )
    assert not flotantes, f"vuelve el upstream flotante {FLOTANTE}: {flotantes}"
    grupos = {e["model_name"] for e in config["model_list"]}
    assert "alibaba-dsv41-fl" not in grupos, "el flotante volvio al model_list"


def test_los_dos_nombres_viejos_resuelven_al_pin(config):
    alias = (config.get("router_settings") or {}).get("model_group_alias") or {}
    for nombre in ALIAS_AL_PIN:
        assert alias.get(nombre) == PIN, f"{nombre} -> {alias.get(nombre)!r}, esperado {PIN}"
    grupos = {e["model_name"] for e in config["model_list"]}
    for nombre in ALIAS_AL_PIN:
        assert alias[nombre] in grupos, f"{nombre} apunta a un grupo que no existe"
        assert alias[nombre] not in alias, f"{nombre} encadena alias: {alias[nombre]}"


def test_el_pin_conserva_sus_dos_deployments(config):
    deps = [e for e in config["model_list"] if e["model_name"] == PIN]
    assert sorted(e["model_info"]["id"] for e in deps) == [f"{PIN}-k1", f"{PIN}-k2"]
    assert {e["litellm_params"]["model"] for e in deps} == {"openai/deepseek-v4.1-flash-1017"}
