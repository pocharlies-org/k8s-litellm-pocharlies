from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def test_dense_uncensored_is_direct_single_backend_without_fallback():
    text = MANIFEST.read_text()
    assert 'QWEN36_27B_UNCENSORED_ALIASES = ("dense-uncensored",)' in text
    assert text.count('"aliases": QWEN36_27B_UNCENSORED_ALIASES') == 1
    assert '"id_prefix": "dgx2-qwen36-27b-uncensored-nvfp4"' in text

    router_block = text[text.index("ROUTER_MODELS ="):text.index("MODEL_MAP =")]
    assert "dense-uncensored" not in router_block
    model_map_block = text[text.index("MODEL_MAP ="):text.index("class StripUnsupportedParams")]
    assert "dense-uncensored" not in model_map_block


def test_manifest_stays_valid_yaml():
    list(yaml.safe_load_all(MANIFEST.read_text()))
