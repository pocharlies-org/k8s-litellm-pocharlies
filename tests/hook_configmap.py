"""Carga el fuente del hook `strip_params` del ConfigMap (los contratos de sellos
lo ejecutan con ast; cada contrato se queda solo su trozo)."""
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in (x for x in yaml.safe_load_all(MANIFEST.read_text()) if x)
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
