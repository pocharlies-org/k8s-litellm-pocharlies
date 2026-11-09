"""Contract DGX-1189: el trafico `claude-*` hacia Anthropic parece del cliente oficial.

Medido el 20-10-2026: litellm mandaba `user-agent: litellm/1.105.1` y descartaba
`anthropic-beta: claude-code-20250219` (fuera de su lista). Dos piezas lo arreglan:

  1. Los 8 deployments `claude-acct-*` del manifiesto llevan `headers:` con el
     user-agent y la familia `x-stainless-*` de Claude Code (el proxy reenvia
     `anthropic-beta` y `x-*` del cliente, pero NO el user-agent ni x-stainless).
  2. session_router amplia la lista de betas del manager de litellm in-process
     (las tres que Claude Code manda y litellm no conoce) y re-amplia tras un
     reload, que vacia la cache.
  3. Las cabeceras las genera dev/claude_fingerprints.py con UN perfil de maquina por
     cuenta (ocho huellas distintas) y CLAUDE_CLI_VERSION como unica fuente de la
     version. Antes de subir el CLI se captura su cabecera real con
     dev/capture_claude_cli_headers.py; si el manifiesto va detras, fallan los tests.
  4. Y no solo el config con el que arranca el proceso: test_el_cable_... mide lo que
     litellm pone en el egress hacia api.anthropic.com (donde haya litellm; en CI se salta).
"""
import pathlib
import sys
import types

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
DEV = ROOT / "dev" / "session_router.py"
sys.path.insert(0, str(DEV.parent))
import capture_claude_cli_headers as cap  # noqa: E402
import claude_fingerprints as fp  # noqa: E402

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


def test_el_user_agent_va_atras_del_cli_instalado():
    """El UA lo fija dev/claude_fingerprints.py: si sube el CLI y nadie actualiza, esto falla."""
    ua_esperada = f"claude-cli/{fp.CLAUDE_CLI_VERSION} (external, cli)"
    for dep in _deployments():
        headers = dep["litellm_params"].get("headers") or {}
        assert headers.get("user-agent") == ua_esperada, (
            f"{dep['model_info']['id']}: UA {headers.get('user-agent')!r} != {ua_esperada!r} "
            f"(captura real con dev/capture_claude_cli_headers.py y corre --write)")


def test_los_ocho_fingerprints_son_distintos_y_verosimiles():
    """Ocho cuentas con la misma huella no son un fleet de usuarios: cada perfil es una maquina."""
    huellas = {}
    for dep in _deployments():
        headers = dep["litellm_params"].get("headers") or {}
        huella = {k: headers.get(k) for k in
                  ("x-stainless-os", "x-stainless-arch", "x-stainless-runtime-version",
                   "x-stainless-package-version", "x-stainless-timeout")}
        assert huella["x-stainless-os"] in fp.OS_VALIDOS, dep["model_info"]["id"]
        assert huella["x-stainless-arch"] in fp.ARCH_VALIDOS, dep["model_info"]["id"]
        huellas[dep["model_info"]["id"]] = tuple(sorted(huella.items()))
    duplicadas = {h for h in huellas.values() if list(huellas.values()).count(h) > 1}
    assert not duplicadas, f"fingerprints repetidos en {len(duplicadas)} huellas: {sorted(huellas)}"
    # y en el generador, que es donde se escribe: dos perfiles iguales no deben colarse
    gen = {tuple(sorted(h.items())) for h in fp.fingerprints().values()}
    assert len(gen) == len(fp.CUENTAS), "dos cuentas con el mismo perfil en dev/claude_fingerprints.py"


def test_el_manifiesto_sincronizado_con_el_generador():
    _, cambios = fp.sync(MANIFEST.read_text())
    assert not cambios, f"fuera de sincronia (corre --write): {', '.join(cambios)}"


def test_el_cable_lleva_el_fingerprint_de_cada_cuenta():
    """Lo que ve Anthropic, no solo el config con el que arranco el proceso.

    Mide el egress: los 8 deployments del manifiesto contra un sink que hace de
    api.anthropic.com, pasando por el Router de litellm. Necesita litellm, que en CI no
    esta (el lock de contratos es minimo); se corre donde si lo hay, o a mano en el pod:
      kubectl exec -n litellm <pod> -c litellm -- python3 -I /tmp/fp/... --cable
    """
    try:
        from litellm import Router  # noqa: F401
    except ImportError:
        # importorskip no vale: sin el paquete hay un `litellm` de namespace (unknown
        # location) que pasa por importable y luego no tiene Router.
        pytest.skip("el cable necesita el litellm del proxy")
    assert cap.cable() == 0, "la cabecera no llega al egress como la fija el contrato"


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
