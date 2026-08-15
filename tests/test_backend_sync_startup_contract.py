import re
from pathlib import Path


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _required_team_models(text):
    """Los nombres de OPENCLAW_TEAM_REQUIRED_MODELS, como conjunto.

    La lista CRECE a proposito (el reconciliador hace union aditiva), asi que
    fijar el literal entero convertia cada ampliacion en un test roto sin
    contarnos nada. Lo que se contrasta es pertenencia: que estan los que tienen
    que estar y NO estan los que no pueden existir.
    """
    match = re.search(r'OPENCLAW_TEAM_REQUIRED_MODELS, value: "([^"]*)"', text)
    assert match, "el manifest ya no declara OPENCLAW_TEAM_REQUIRED_MODELS"
    return {name.strip() for name in match.group(1).split(",") if name.strip()}


def test_ready_grace_keeps_existing_models_during_controller_restart():
    text = MANIFEST.read_text()
    main_start = text.index("    def main() -> None:")
    main_end = text.index("\n\n    if __name__ == \"__main__\":", main_start)
    main_block = text[main_start:main_end]

    assert "if now - (ready_since[name] or now) < READY_GRACE_SECONDS:" in main_block
    assert "continue" in main_block
    assert "desired = True" in main_block


def test_openclaw_team_has_canary_and_explicit_operational_routes():
    text = MANIFEST.read_text()

    required = _required_team_models(text)
    # 2026-07-27: qwen36-27b-nvfp4-v024-f2-dgx1 was dropped -- that deployment
    # was deleted on 07-26, so granting the team access to it was a permission
    # for a model that cannot exist.
    assert "qwen36-27b-nvfp4-v024-f2-dgx1" not in required
    assert {
        "ornith-canary", "ornith-1.0", "qwen38-27b",
        "router", "agent", "high", "max",
    } <= required
    assert 'OPENCLAW_KEY_ALIAS, value: "openclaw-qwen36-prod"' in text
