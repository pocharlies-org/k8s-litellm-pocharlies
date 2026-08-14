"""Reasoning metadata published for the live DeepSeek backend.

DeepSeek's compatibility aliases (tooling/agent/high/max) remain available for
clients that can only choose a model name. OpenCode and OpenChamber can send a
reasoning effort, so the direct model must advertise its real tiers and let the
client render one model with variants instead of four apparent checkpoints.
"""

import ast
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _sync_code() -> str:
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for content in (doc.get("data") or {}).values():
            if "BACKENDS = (" in content and "managed_model_contract" in content:
                return content
    raise AssertionError("no encuentro el codigo del backend-sync en el manifiesto")


def _deepseek_backend() -> dict:
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
                    entry[key.value] = "<dinamico>"
            if entry.get("name") == "deepseek-v4-flash-tp2":
                return entry
    raise AssertionError("no encuentro deepseek-v4-flash-tp2 en BACKENDS")


def test_deepseek_publica_sus_tiers_reales():
    backend = _deepseek_backend()
    assert backend["supports_reasoning"] is True
    assert backend["supported_reasoning_efforts"] == (
        "none",
        "low",
        "high",
        "max",
    )


def test_el_reconciler_refresca_reasoning_y_efforts():
    code = _sync_code()
    desired_start = code.index("def desired_deployments")
    desired = code[desired_start:code.index("def current_models_by_id")]
    assert '"supports_reasoning": bool(backend.get("supports_reasoning", False))' in desired
    assert 'backend.get("supported_reasoning_efforts", ())' in desired

    contract_start = code.index("def managed_model_contract")
    contract = code[contract_start:code.index("def add_model")]
    assert '"supports_reasoning": bool(info.get("supports_reasoning", False))' in contract
    assert 'info.get("supported_reasoning_efforts") or ()' in contract
