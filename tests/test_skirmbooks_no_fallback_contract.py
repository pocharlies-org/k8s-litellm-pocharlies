"""SKIRM-400: la key `skirmbooks` (backend de Skirmbooks: OCR y validador fiscal) sale a prioridad -10 y no degrada a la nube.

Se carga el hook REAL del manifiesto, como test_analisis_no_fallback_contract.py.
"""
from test_aurora_fallback_policy_contract import _policy, hook  # noqa: F401
import yaml

from test_priority_injection_contract import CONFIG_YAML, _data, _mod_with_config


def test_la_clave_de_skirmbooks_esta_sellada(hook):  # noqa: F811
    request, disabled = _policy(hook, "skirmbooks", {"disable_fallbacks": False})

    assert disabled is True
    assert request == {"disable_fallbacks": True}
    assert "skirmbooks" in hook.NO_FALLBACK_KEY_ALIASES


def test_skirmbooks_y_su_ui_al_residente_salen_a_prioridad_menos_10(tmp_path):
    mod, _ = _mod_with_config(tmp_path, content=yaml.safe_dump(CONFIG_YAML))
    for alias in ("skirmbooks", "skirmbooks-ui"):
        d = _data("qwen38-flash-next", alias)
        mod.apply_priority(d)
        assert d["extra_body"]["priority"] == -10, alias


def test_skirmbooks_a_alibaba_no_inyecta_prioridad(tmp_path):
    mod, _ = _mod_with_config(tmp_path, content=yaml.safe_dump(CONFIG_YAML))
    d = _data("alibaba-q38-flash", "skirmbooks")
    mod.apply_priority(d)
    assert "extra_body" not in d
