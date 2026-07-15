from pathlib import Path


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def test_ready_grace_keeps_existing_models_during_controller_restart():
    text = MANIFEST.read_text()
    main_start = text.index("    def main() -> None:")
    main_end = text.index("\n\n    if __name__ == \"__main__\":", main_start)
    main_block = text[main_start:main_end]

    assert "if now - (ready_since[name] or now) < READY_GRACE_SECONDS:" in main_block
    assert "continue" in main_block
    assert "desired = True" in main_block


def test_openclaw_team_has_logical_and_explicit_operational_routes():
    text = MANIFEST.read_text()

    assert (
        'OPENCLAW_TEAM_REQUIRED_MODELS, value: '
        '"tooling,dense,dense-reasoning,ornith-canary,ornith-1.0,dense-uncensored,'
        'qwen36-27b-nvfp4-v024-f2-dgx1"'
    ) in text
    assert 'OPENCLAW_KEY_ALIAS, value: "openclaw-qwen36-prod"' in text
