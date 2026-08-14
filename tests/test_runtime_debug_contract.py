#!/usr/bin/env python3
"""Keep verbose LiteLLM request debugging disabled in production."""

from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def test_litellm_does_not_start_with_detailed_debug():
    deployments = [
        doc
        for doc in yaml.safe_load_all(MANIFEST.read_text())
        if doc and doc.get("kind") == "Deployment"
        and doc.get("metadata", {}).get("name") == "litellm"
    ]
    assert len(deployments) == 1

    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    litellm = next(container for container in containers if container["name"] == "litellm")
    assert "--detailed_debug" not in litellm.get("args", [])
