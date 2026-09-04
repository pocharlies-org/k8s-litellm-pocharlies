"""Permanent tombstone for the removed automatic-routing model surface."""
import ast
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
RETIRED = {"router", "auto", "litellmrouter", "agent"}


def _documents():
    return [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]


def _configmap_source(name, key):
    return next(
        doc["data"][key]
        for doc in _documents()
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == name
    )


def test_retired_names_are_not_published_or_fallback_targets():
    raw = _configmap_source("litellm-config", "config.yaml")
    config = yaml.safe_load(raw)
    published = {entry["model_name"] for entry in config.get("model_list", [])}
    assert RETIRED.isdisjoint(published)

    edges = config.get("router_settings", {}).get("fallbacks") or []
    fallback_names = {
        name
        for edge in edges
        for source, targets in edge.items()
        for name in (source, *(targets or []))
    }
    assert RETIRED.isdisjoint(fallback_names)


def test_retired_names_are_not_registered_by_runtime_code():
    for name, key in (
        ("litellm-config", "litellm_strip_params.py"),
        ("litellm-dgx-backend-sync", "sync.py"),
    ):
        source = _configmap_source(name, key)
        exact_literals = {
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert RETIRED.isdisjoint(exact_literals), name


def test_classifier_implementation_is_gone():
    tree = ast.parse(_configmap_source("litellm-config", "litellm_strip_params.py"))
    definitions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assignments = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "_classify_route" not in definitions
    assert "AUTO_ROUTED_MODELS" not in assignments
    assert "ROUTE" not in assignments


def test_watchdog_uses_tooling():
    text = (ROOT / "k8s" / "litellm-watchdog-cron.yaml").read_text()
    assert json.dumps("tooling") in text
    assert all(json.dumps(name) not in text for name in RETIRED)
