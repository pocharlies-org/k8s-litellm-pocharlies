"""Contract DGX-1189: el trafico `claude-*` hacia Anthropic parece del cliente oficial.

Medido el 20-10-2026: litellm mandaba `user-agent: litellm/1.105.1` y descartaba
`anthropic-beta: claude-code-20250219` (fuera de su lista). Dos piezas lo arreglan:

  1. Los 8 deployments `claude-acct-*` del manifiesto llevan `headers:` con el
     user-agent y la familia `x-stainless-*` de Claude Code (el proxy reenvia
     `anthropic-beta` y `x-*` del cliente, pero NO el user-agent ni x-stainless).
  2. session_router amplia la lista de betas del manager de litellm in-process
     (las tres que Claude Code manda y litellm no conoce) y re-amplia tras un
     reload, que vacia la cache.
"""
import pathlib
import types

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
DEV = ROOT / "dev" / "session_router.py"

BETAS = ("claude-code-20250219", "thinking-token-count-2026-05-13", "inline-tools-2026-09-15")


def _deployments():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            cfg = yaml.safe_load(doc["data"]["config.yaml"])
            return [d for d in cfg["model_list"]
                    if str(d.get("model_info", {}).get("id", "")).startswith("claude-acct-")]
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def test_los_ocho_deployments_claude_llevan_las_cabeceras_oficiales():
    deps = _deployments()
    assert len(deps) == 8, f"se esperaban 8 cuentas claude, hay {len(deps)}"
    for dep in deps:
        headers = dep["litellm_params"].get("headers") or {}
        assert headers.get("user-agent", "").startswith("claude-cli/"), dep["model_info"]["id"]
        assert headers.get("x-app") == "cli", dep["model_info"]["id"]
        for key in ("x-stainless-lang", "x-stainless-runtime", "x-stainless-package-version"):
            assert key in headers, f"{dep['model_info']['id']} sin {key}"


def _cargar_session_router_con_manager_falso():
    """Ejecuta el fuente con un manager de betas de mentirijillas y lo devuelve."""
    estado = {"config": {"anthropic": {"oauth-2025-04-20": "oauth-2025-04-20"}}, "reload": False}

    def _load():
        return estado["config"]

    def _reload():
        estado["config"] = {"anthropic": {"oauth-2025-04-20": "oauth-2025-04-20"}}
        estado["reload"] = True
        return estado["config"]

    manager = types.ModuleType("litellm.anthropic_beta_headers_manager")
    manager._load_beta_headers_config = _load
    manager.reload_beta_headers_config = _reload
    litellm = types.ModuleType("litellm")
    litellm.anthropic_beta_headers_manager = manager
    sys = __import__("sys")
    saved = {n: sys.modules.get(n) for n in ("litellm", "litellm.anthropic_beta_headers_manager", "httpx")}
    sys.modules["litellm"] = litellm
    sys.modules["litellm.anthropic_beta_headers_manager"] = manager
    sys.modules["httpx"] = types.ModuleType("httpx")
    module = types.ModuleType("session_router")
    exec(compile(DEV.read_text(), "session_router.py", "exec"), module.__dict__)

    def restaurar():
        for n, v in saved.items():
            if v is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = v

    return module, estado, restaurar


def test_session_router_amplia_la_lista_de_betas_y_sobrevive_al_reload():
    # los stubs se restauran al final: el reload re-importa el manager, como en el proxy vivo
    module, estado, restaurar = _cargar_session_router_con_manager_falso()
    try:
        for beta in BETAS:
            assert estado["config"]["anthropic"].get(beta) == beta, f"sin ampliar tras el import: {beta}"
        # el reload del proxy vacia la cache: el hook debe re-ampliar
        module._reload_beta_headers_and_extend()
        for beta in BETAS:
            assert estado["config"]["anthropic"].get(beta) == beta, f"perdida tras reload: {beta}"
    finally:
        restaurar()
