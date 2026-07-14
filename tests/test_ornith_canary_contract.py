from pathlib import Path


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def test_ornith_canary_is_one_readiness_gated_backend_without_fallback():
    text = MANIFEST.read_text()

    assert "ORNITH_CANARY_ALIASES = (" in text
    alias_block = text[
        text.index("ORNITH_CANARY_ALIASES = ("):
        text.index("QWEN36_COMPAT_ALIASES = (")
    ]
    assert '"ornith-canary"' in alias_block
    assert '"ornith-1.0"' in alias_block
    assert '"tooling"' not in alias_block
    assert text.count('"aliases": ORNITH_CANARY_ALIASES') == 1
    assert '"id_prefix": "dgx1-ornith-35b-nvfp4-mtp"' in text
    assert '"context_window": 65536' in text
    assert '"max_parallel_requests": 4' in text

    router_block = text[text.index("ROUTER_MODELS ="):text.index("MODEL_MAP =")]
    model_map_block = text[
        text.index("MODEL_MAP ="):text.index("class StripUnsupportedParams")
    ]
    assert "ornith-canary" not in router_block
    assert "ornith-1.0" not in router_block
    assert "ornith-canary" not in model_map_block
    assert "ornith-1.0" not in model_map_block


def test_ornith_model_metadata_uses_backend_specific_context():
    text = MANIFEST.read_text()
    desired = text[
        text.index("def desired_deployments"):
        text.index("def add_model")
    ]
    assert 'backend.get("max_tokens", 32768)' in desired
    assert 'backend.get("context_window", 262144)' in desired
