"""Helpers compartidos para leer k8s/manifest.yaml desde los tests.

Viven aqui (y no repetidos en cada test) porque el detector de duplicados de
la org (jscpd, >=12 lineas) cuenta el par `MANIFEST` + `_docs()` + `_named()`
como fragmento copiado en cuanto un test nuevo lo vuelve a escribir literal.
"""
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def named(kind, name):
    for doc in docs():
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"no encuentro {kind}/{name}")


def config_data(name="litellm-config"):
    return named("ConfigMap", name)["data"]
