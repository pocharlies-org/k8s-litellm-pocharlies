"""El lane barato `qwen35-4b-fast` existe, apunta al 4B y solo lo pide la key de
OpenClaw (SC-131: el background review de los perfiles Hermes).

POR QUE EXISTE ESTE FICHERO. El alias nuevo comparte backend con `qwen35-4b` a
proposito; lo que cambia es quien puede pedirlo. Como el allowlist de la key no
puede cerrar nada (`models: []` = todos), la unica capa es el gate del hook, y
ese gate tiene que fallar CERRADO. Si alguien borra la variable del deployment,
el lane se queda sin consumidores visibles — el fallo seguro — y no abierto a
las keys que procesan datos de terceros.
"""
import ast
import types
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"

WANT_FN = {"_qwen35_4b_fast_access_denied"}
WANT_CONST = {"QWEN35_4B_FAST_ALIAS", "QWEN35_4B_FAST_ALLOWED_KEY_ALIASES"}

ALLOWED_KEY = "openclaw-qwen36-prod"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _config():
    cm = next(
        d for d in _docs()
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    return cm, yaml.safe_load(cm["data"]["config.yaml"])


def _deployment_env():
    dep = next(
        d for d in _docs()
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm"
    )
    container = next(
        c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "litellm"
    )
    return {e["name"]: e for e in container["env"]}


def _gate(env):
    cm, _ = _config()
    tree = ast.parse(cm["data"]["litellm_strip_params.py"])
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("fastlane")
    mod.os = types.SimpleNamespace(environ=dict(env))
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


def test_the_alias_exists_and_points_at_the_4b_backend():
    _, cfg = _config()
    entry = next(m for m in cfg["model_list"] if m["model_name"] == "qwen35-4b-fast")
    base = next(m for m in cfg["model_list"] if m["model_name"] == "qwen35-4b")
    assert entry["litellm_params"]["model"] == base["litellm_params"]["model"]
    assert entry["litellm_params"]["api_base"] == base["litellm_params"]["api_base"]
    assert entry["model_info"]["supports_function_calling"] is True


def test_a_deployment_without_the_variable_denies_everyone():
    gate = _gate({})
    for key in (ALLOWED_KEY, "master", "synapse", "document-intake"):
        denied, detail = gate._qwen35_4b_fast_access_denied("qwen35-4b-fast", key)
        assert denied, f"{key} deberia estar denegada con la lista vacia"
        assert detail["allowed"] == []


def test_the_allowed_key_gets_through_and_nobody_else_does():
    gate = _gate({"LITELLM_QWEN35_4B_FAST_ALLOWED_KEYS": ALLOWED_KEY})
    denied, _ = gate._qwen35_4b_fast_access_denied("qwen35-4b-fast", ALLOWED_KEY)
    assert not denied
    for key in ("master", "synapse", "document-intake", "opencode-20260630-local"):
        denied, detail = gate._qwen35_4b_fast_access_denied("qwen35-4b-fast", key)
        assert denied, f"{key} no deberia alcanzar el lane barato"
        assert detail["requested"] == "qwen35-4b-fast"


def test_the_gate_ignores_everything_that_is_not_the_fast_lane():
    gate = _gate({"LITELLM_QWEN35_4B_FAST_ALLOWED_KEYS": ALLOWED_KEY})
    for model in ("tooling", "qwen35-4b", "qwen35-4b-uncensored", "", None):
        denied, _ = gate._qwen35_4b_fast_access_denied(model, "synapse")
        assert not denied, f"{model!r} no es el lane barato y no le toca a este gate"


def test_the_deployment_ships_a_non_empty_allowlist():
    env = _deployment_env()
    value = env["LITELLM_QWEN35_4B_FAST_ALLOWED_KEYS"]["value"]
    assert value.strip(), "lista vacia = lane inservible (ver el test de fail-closed)"
    assert ALLOWED_KEY in [item.strip() for item in value.split(",")]
