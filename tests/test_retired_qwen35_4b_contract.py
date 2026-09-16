"""Permanent tombstone for the retired `qwen35-4b` 4B llama.cpp backend (SC-384).

The `qwen35-4b` / `qwen35-4b-fast` / `qwen3.5-4b` aliases pointed at the
`qwen35-4b-int4` backend on the x86 RTX. That backend is down (every request
returns Connection error, 0 replicas ready) and is no longer needed: OpenClaw
runs on the `tooling` capacity alias, and the dashboard consumers that named the
4B are repointed to `tooling` in dgx-infra. These names must stay out of the
model_list, the router group-alias map, and the pre-call hook.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
RETIRED = {"qwen35-4b", "qwen35-4b-fast", "qwen3.5-4b"}


def _configmap() -> dict:
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return doc["data"]
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def test_retired_4b_names_are_not_published():
    config = yaml.safe_load(_configmap()["config.yaml"])
    published = {entry["model_name"] for entry in config.get("model_list", [])}
    assert RETIRED.isdisjoint(published)


def test_retired_4b_names_are_not_group_aliases():
    config = yaml.safe_load(_configmap()["config.yaml"])
    aliases = set(config.get("router_settings", {}).get("model_group_alias", {}) or {})
    assert RETIRED.isdisjoint(aliases)


def test_fast_lane_gate_and_env_are_gone():
    for key, value in _configmap().items():
        assert "_qwen35_4b_fast_access_denied" not in value, key
        assert "LITELLM_QWEN35_4B_FAST_ALLOWED_KEYS" not in value, key
