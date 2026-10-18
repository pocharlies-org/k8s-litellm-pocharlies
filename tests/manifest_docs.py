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


def hook_gate(want_fn, want_const, env, name="hookgate"):
    """Ejecuta aislados los gates por prefijo del hook (litellm_strip_params.py).

    Extrae solo las funciones `want_fn` y las asignaciones `want_const` del modulo
    y las corre con un `os.environ` falso: asi el test mide el gate tal como va en
    el ConfigMap sin importar litellm. Compartido por los gates grok-* y gpt-*.
    """
    import ast
    import types

    tree = ast.parse(config_data()["litellm_strip_params.py"])
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in want_fn)
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in want_const for t in n.targets))
    ]
    missing = set(want_fn) - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType(name)
    mod.os = types.SimpleNamespace(environ=dict(env))
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod
