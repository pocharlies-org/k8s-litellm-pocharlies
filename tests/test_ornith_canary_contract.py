from pathlib import Path


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def test_ornith_tooling_is_one_readiness_gated_terminal_backend():
    text = MANIFEST.read_text()

    assert "ORNITH_TOOLING_ALIASES = (" in text
    alias_block = text[
        text.index("ORNITH_TOOLING_ALIASES = ("):
        text.index("QWEN36_COMPAT_ALIASES = (")
    ]
    assert '"tooling"' in alias_block
    assert '"ornith-canary"' in alias_block
    assert '"ornith-1.0"' in alias_block
    assert text.count('"aliases": ORNITH_TOOLING_ALIASES') == 1
    assert '"id_prefix": "dgx1-ornith-35b-nvfp4-mtp-256k"' in text
    assert '"max_input_tokens": 262144' in text
    assert '"context_window": 262144' in text
    assert '"max_parallel_requests": 4' in text
    assert '"supports_parallel_function_calling": False' in text

    router_block = text[text.index("ROUTER_MODELS ="):text.index("MODEL_MAP =")]
    model_map_block = text[
        text.index("MODEL_MAP ="):text.index("COMPACTION_LOCAL_ALIAS =")
    ]
    assert "ornith-canary" not in router_block
    assert "ornith-1.0" not in router_block
    assert '"tooling"' not in router_block
    assert '"compaction-local"' not in router_block
    assert "ornith-canary" not in model_map_block
    assert "ornith-1.0" not in model_map_block
    assert '"tooling"' not in model_map_block

    qwen_alias_block = text[
        text.index("QWEN36_COMPAT_ALIASES = ("):
        text.index("QWEN36_27B_DENSE_ALIASES = (")
    ]
    assert '"tooling"' not in qwen_alias_block


def test_ornith_model_metadata_uses_backend_specific_context():
    text = MANIFEST.read_text()
    desired = text[
        text.index("def desired_deployments"):
        text.index("def add_model")
    ]
    assert 'backend.get("max_tokens", 32768)' in desired
    assert 'backend.get(\n                            "max_input_tokens",' in desired
    assert 'backend.get("context_window", 262144)' in desired
