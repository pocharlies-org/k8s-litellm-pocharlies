"""claude-cloudblue: un `claude-*` solo lo sirve Anthropic (dgx.litellm.claude-anthropic-only.v1).

POR QUE EXISTE (23-10-2026, orden de Dani: «esa key no tiene que tener ningun
fallback a alibaba»). `claude-cloudblue` es la key del Claude Code del Mac, con
datos de CloudBlue. El sello de NO_FALLBACK_KEY_ALIASES (`disable_fallbacks`)
cierra los fallbacks del Router, pero no los REWRITES del propio hook: el
claude_gate de la compania (session_router._company_claude) reescribe un
`claude-*` con `x-claude-class: company` a su `target` (qwen38-flash-next) aunque
la key este sellada, y esa cabecera la puede mandar cualquier cliente con la key.

Que afirma cada test:
  * el gate por si solo: claude-cloudblue + `claude-*` reescrito -> denegado;
    sin rewrite, otras keys o modelos no-claude -> pasa;
  * que esta ALAMBRADO en el async_pre_call_hook y es lo ULTIMO antes del
    `return data`, despues de la politica de la compania, del desvio de vision
    y de apply_session_routing: un invariante que se pueda reordenar por delante
    de un rewrite no es un invariante;
  * que el grupo `claude-*` del model_list solo tiene deployments `anthropic/`.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _cm_data():
    return next(
        d["data"] for d in _docs()
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def hook():
    tree = ast.parse(_cm_data()["litellm_strip_params.py"])
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name == "_claude_rewrite_denied")
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "ANTHROPIC_ONLY_CLAUDE_KEY_ALIASES" for t in n.targets))
    ]
    assert len(keep) == 2, "el hook ya no define _claude_rewrite_denied / ANTHROPIC_ONLY_CLAUDE_KEY_ALIASES"
    module = types.ModuleType("claude_anthropic_only")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), module.__dict__)
    return module


def test_claude_cloudblue_esta_en_la_lista(hook):
    assert "claude-cloudblue" in hook.ANTHROPIC_ONLY_CLAUDE_KEY_ALIASES


@pytest.mark.parametrize("target", ["qwen38-flash-next", "alibaba-q38-flash", "alibaba-ds41f1017", "tooling", ""])
def test_un_claude_reescrito_se_deniega(hook, target):
    denied, detail = hook._claude_rewrite_denied("claude-sonnet-5-5", target, "claude-cloudblue")
    assert denied is True
    assert detail["error"] == "claude_anthropic_only"
    assert detail["requested"] == "claude-sonnet-5-5"
    assert detail["rewritten_to"] == target


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
def test_un_claude_sin_rewrite_pasa(hook, model):
    assert hook._claude_rewrite_denied(model, model, "claude-cloudblue") == (False, None)


def test_un_modelo_local_pedido_a_mano_pasa(hook):
    assert hook._claude_rewrite_denied(
        "qwen38-flash-next", "qwen38-flash-next", "claude-cloudblue") == (False, None)


@pytest.mark.parametrize("key", ["claude-x86", "hermes", "claude-mac", "unknown", "master"])
def test_otras_keys_conservan_el_gate_de_la_compania(hook, key):
    assert hook._claude_rewrite_denied("claude-sonnet-5-5", "qwen38-flash-next", key) == (False, None)
    assert key not in hook.ANTHROPIC_ONLY_CLAUDE_KEY_ALIASES


def _pre_call_hook_source():
    tree = ast.parse(_cm_data()["litellm_strip_params.py"])
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_pre_call_hook":
            src = ast.get_source_segment(_cm_data()["litellm_strip_params.py"], node)
            if "_company_policy" in src:
                return node, src
    raise AssertionError("no encuentro el async_pre_call_hook de litellm_strip_params")


def test_el_gate_es_lo_ultimo_antes_del_return():
    node, src = _pre_call_hook_source()
    gate = src.index("_claude_rewrite_denied(")
    for earlier in ("_company_policy(data, requested_model)", "_vision_target(", "apply_session_routing("):
        assert earlier in src, f"el hook ya no llama {earlier}; revisa el orden del gate"
        assert src.index(earlier) < gate, f"{earlier} va DESPUES del gate anthropic-only"
    tail = src[gate:]
    assert "raise HTTPException(status_code=403" in tail
    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
    assert len(returns) == 1, "un return temprano saltaria el gate"
    assert src.rindex("return data") > gate


def test_el_grupo_claude_solo_tiene_anthropic():
    config = yaml.safe_load(_cm_data()["config.yaml"])
    claude = [m for m in config["model_list"] if m["model_name"].startswith("claude")]
    assert claude, "no hay grupo claude-* en el model_list"
    for dep in claude:
        assert str(dep["litellm_params"]["model"]).startswith("anthropic/"), dep["model_info"].get("id")
        assert not dep["litellm_params"].get("api_base"), dep["model_info"].get("id")
    fallbacks = (config.get("router_settings") or {}).get("fallbacks") or []
    for edge in fallbacks:
        for src in edge:
            assert not src.startswith("claude"), f"fallback del Router desde {src}"
    ls = config.get("litellm_settings") or {}
    for k in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
        assert not ls.get(k), f"litellm_settings.{k} no esta vacio"
