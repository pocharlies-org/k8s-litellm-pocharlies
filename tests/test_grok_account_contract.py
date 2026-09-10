"""La cuenta Grok (xAI) por litellm: tres alias, un gate fail-closed y un modulo.

POR QUE EXISTE ESTE FICHERO (10-09-2026). Los alias `grok-*` gastan DINERO de una
cuenta personal (creditos de xAI) y no hay API de saldo que acepte el token OAuth,
asi que el saldo local es el max_budget de la key `grok-account` y lo unico que
impide que otra key lo vacie es el hook: `models: []` significa TODOS los modelos
en 7 de las 10 keys vivas. Como en or-*, el gate va por PREFIJO y nace cerrado.

Y el modulo xai_account_provider.py no es decorativo: sin el, /v1/images/generations
saldria sin token (litellm solo cablea use_xai_oauth en chat) y /v1/videos diria
"video generation is not supported for xai". Si alguien lo quita del ConfigMap, de
los callbacks o del volumeMount, los alias siguen en el catalogo y fallan en vivo.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"

WANT_FN = {"_grok_access_denied"}
WANT_CONST = {"GROK_ALIAS_PREFIX", "GROK_ALLOWED_KEY_ALIASES"}
ALLOWED_KEYS = ("grok-account", "grok-probe")
GROK_ALIASES = {"grok-imagine", "grok-imagine-video", "grok-probe"}
MODULE = "xai_account_provider.py"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _config():
    cm = next(d for d in _docs() if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    return cm, yaml.safe_load(cm["data"]["config.yaml"])


def _litellm_container():
    dep = next(d for d in _docs() if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm")
    spec = dep["spec"]["template"]["spec"]
    return spec, next(c for c in spec["containers"] if c["name"] == "litellm")


def _gate(env):
    cm, _ = _config()
    tree = ast.parse(cm["data"]["litellm_strip_params.py"])
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("grokgate")
    mod.os = types.SimpleNamespace(environ=dict(env))
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


@pytest.fixture(scope="module")
def open_gate():
    return _gate({"LITELLM_GROK_ALLOWED_KEYS": ",".join(ALLOWED_KEYS)})


def test_a_deployment_without_the_variable_denies_everyone():
    gate = _gate({})
    for key in ALLOWED_KEYS + ("master", "dgx-dashboard", "codex"):
        denied, detail = gate._grok_access_denied("grok-imagine", key)
        assert denied, f"{key} paso con la variable vacia"
        assert detail["allowed"] == []


def test_only_the_account_keys_get_through(open_gate):
    for alias in GROK_ALIASES:
        for key in ALLOWED_KEYS:
            assert open_gate._grok_access_denied(alias, key) == (False, None)
        for key in ("master", "dgx-dashboard", "codex", "document-intake", None):
            denied, _ = open_gate._grok_access_denied(alias, key)
            assert denied, f"{key!r} paso a {alias}"


def test_an_alias_that_does_not_exist_yet_is_already_closed(open_gate):
    denied, _ = open_gate._grok_access_denied("grok-imagine-4k-2027", "dgx-dashboard")
    assert denied


def test_the_gate_ignores_everything_that_is_not_a_grok_alias(open_gate):
    for alias in ("tooling", "qwen38-flash-next", "or-glm", "gpt-5.6-sol", "", None):
        assert open_gate._grok_access_denied(alias, "document-intake") == (False, None)


def test_the_hook_calls_the_gate_in_the_pre_call_chain():
    cm, _ = _config()
    assert "_denied, _detail = _grok_access_denied(" in cm["data"]["litellm_strip_params.py"]


def test_the_deployment_ships_the_allowlist_with_both_keys():
    _, c = _litellm_container()
    env = {e["name"]: e for e in c["env"]}
    value = env["LITELLM_GROK_ALLOWED_KEYS"]["value"]
    assert set(v.strip() for v in value.split(",")) == set(ALLOWED_KEYS)


def test_the_three_account_aliases_exist_and_carry_a_price():
    _, cfg = _config()
    by_name = {m["model_name"]: m for m in cfg["model_list"]}
    assert GROK_ALIASES <= set(by_name)
    for name in GROK_ALIASES:
        assert by_name[name]["litellm_params"]["model"].startswith("xai/"), name
        # Sin api_key: el token es OAuth. `not-used` haria que el chat se saltara el OAuth.
        assert "api_key" not in by_name[name]["litellm_params"], name
    assert by_name["grok-imagine"]["model_info"]["input_cost_per_image"] > 0
    assert by_name["grok-imagine-video"]["model_info"]["output_cost_per_video_per_second"] > 0
    assert by_name["grok-probe"]["litellm_params"]["use_xai_oauth"] is True
    assert by_name["grok-probe"]["litellm_params"]["max_tokens"] == 1


def test_the_provider_module_is_loaded_mounted_and_registers_video():
    cm, cfg = _config()
    assert MODULE in cm["data"]
    src = cm["data"][MODULE]
    compile(src, MODULE, "exec")
    assert "ProviderConfigManager.get_provider_video_config = staticmethod(" in src
    assert "class XAIVideoConfig(BaseVideoConfig)" in src
    assert f"{MODULE[:-3]}.proxy_handler_instance" in cfg["litellm_settings"]["callbacks"]
    spec, c = _litellm_container()
    mounts = {m.get("subPath"): m["mountPath"] for m in c["volumeMounts"]}
    assert mounts.get(MODULE) == f"/app/{MODULE}"
    assert any(i["name"] == "seed-xai-oauth" for i in spec.get("initContainers", []))
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["XAI_OAUTH_TOKEN_DIR"] == "/var/run/xai-oauth"


def test_no_background_health_check_burns_the_account():
    """background_health_checks esta activo: un alias grok-* con health check
    gastaria saldo cada ronda. Ninguno debe declarar un modo que litellm sondee."""
    _, cfg = _config()
    for m in cfg["model_list"]:
        if m["model_name"] in GROK_ALIASES:
            assert m["model_info"].get("mode") != "chat" or m["model_name"] == "grok-probe"
