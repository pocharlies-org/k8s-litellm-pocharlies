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


def test_openclaw_team_permission_is_reconciled_without_removing_models():
    text = MANIFEST.read_text()
    assert 'OPENCLAW_TEAM_ID, value: "openclaw"' in text
    assert 'OPENCLAW_KEY_ALIAS, value: "openclaw-qwen36-prod"' in text
    assert (
        'OPENCLAW_TEAM_REQUIRED_MODELS, value: '
        '"tooling,compaction-local,dense,dense-reasoning,ornith-canary,ornith-1.0,dense-uncensored,'
        'qwen36-27b-nvfp4-v024-f2-dgx1"'
    ) in text
    block = text[
        text.index("def reconcile_required_team_models"):
        text.index("def managed_model_id")
    ]
    assert 'f"{LITELLM_BASE_URL}/team/info?{query}"' in block
    assert 'f"{LITELLM_BASE_URL}/team/update"' in block
    assert 'desired = list(current)' in block
    assert 'payload={"team_id": OPENCLAW_TEAM_ID, "models": desired}' in block
    assert 'if key.get("key_alias") != OPENCLAW_KEY_ALIAS:' in block
    assert 'f"{LITELLM_BASE_URL}/key/update"' in block
    assert 'payload={"key": key_token, "models": key_desired}' in block
    assert "/team/new" not in block


def test_manifest_stays_valid_yaml():
    list(yaml.safe_load_all(MANIFEST.read_text()))
