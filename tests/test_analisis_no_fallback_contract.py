"""INFRA-988: el alias `analisis` (extraccion y resumen de CDM) sale a prioridad 20 y no degrada a la nube.

Se carga el hook REAL del manifiesto, como test_hindsight_no_fallback_contract.py.
"""
from test_aurora_fallback_policy_contract import _policy, hook  # noqa: F401
import yaml

from test_priority_injection_contract import CONFIG_YAML, _data, _mod_with_config


def test_la_clave_de_analisis_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "analisis", {"disable_fallbacks": False})

    assert disabled is True
    assert request == {"disable_fallbacks": True}
    assert "analisis" in hook.NO_FALLBACK_KEY_ALIASES


def test_analisis_al_residente_sale_a_prioridad_20(tmp_path):
    mod, _ = _mod_with_config(tmp_path, content=yaml.safe_dump(CONFIG_YAML))
    d = _data("qwen38-flash-next", "analisis")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == 20


def test_analisis_a_alibaba_no_inyecta_prioridad(tmp_path):
    mod, _ = _mod_with_config(tmp_path, content=yaml.safe_dump(CONFIG_YAML))
    d = _data("alibaba-q38-flash", "analisis")
    mod.apply_priority(d)
    assert "extra_body" not in d
