"""Contract for the small Qwen3.5 llama.cpp backend on the x86 RTX."""

import ast
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _sync_code() -> str:
    for document in yaml.safe_load_all(MANIFEST.read_text()):
        if document and document.get("kind") == "ConfigMap":
            for content in (document.get("data") or {}).values():
                if "BACKENDS = (" in content and "managed_model_contract" in content:
                    return content
    raise AssertionError("backend-sync ConfigMap not found")


def _qwen_backend() -> dict:
    tree = ast.parse(_sync_code())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(target, "id", "") == "BACKENDS" for target in node.targets):
            continue
        for element in node.value.elts:
            entry = {}
            for key, value in zip(element.keys, element.values):
                if not isinstance(key, ast.Constant):
                    continue
                try:
                    entry[key.value] = ast.literal_eval(value)
                except ValueError:
                    entry[key.value] = "<dynamic>"
            if entry.get("name") == "qwen35-4b-int4":
                return entry
    raise AssertionError("qwen35-4b-int4 missing from BACKENDS")


def test_qwen_backend_uses_its_ready_clusterip_and_one_slot():
    backend = _qwen_backend()
    assert backend["backend"] == "rtx"
    assert backend["id_prefix"] == "rtx-qwen35-4b-int4"
    assert backend["max_parallel_requests"] == 1
    assert backend["max_input_tokens"] == 32768
    assert backend["max_output_tokens"] == 8192


def test_qwen_backend_does_not_overclaim_unverified_capabilities():
    backend = _qwen_backend()
    assert backend["supports_function_calling"] is False
    assert backend["supports_reasoning"] is False
    assert backend["supports_vision"] is False


def test_qwen_aliases_are_specific_and_do_not_take_over_fast():
    code = _sync_code()
    aliases = code[code.index("QWEN35_4B_ALIASES = ("):code.index("# Cluster topology")]
    assert '"qwen35-4b"' in aliases
    assert '"qwen3.5-4b"' in aliases
    assert '"fast"' not in aliases
