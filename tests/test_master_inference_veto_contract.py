"""La master key es solo de administracion: veto de inferencia en el hook.

POR QUE EXISTE (29-09-2026, orden del humano)
---------------------------------------------
El panel de inferencia mostro filas `master` durante semanas: la sonda L2 del
watchdog ("Responde SOLO el numero: 17*23" contra `tooling`, cada 10 min) y,
el 29-09 por la manana, una rafaga de ~90 probes de medicion de censura con la
master sacada del secret. El barrido del 22-08 (LITELLM-MASTER-KEY-DONE) dio
key propia a los siete consumidores de inferencia, pero la master siguio
pudiendo pedir modelos: en LiteLLM la master es key valida para TODO, y
"no se deberia usar" era una convencion, no un control — la misma clase de
garantia que ya se convirtio en lista escrita en git para la ruta abliterada
(ver `UNCENSORED_ALLOWED_KEY_ALIASES` y su comentario).

AQUI SE CONVIERTE EN CONTROL. El `async_pre_call_hook` solo corre para llamadas
de LLM (completion/embedding/rerank/transcription/imagen); las rutas
administrativas —GET /v1/models, /key/list, /health, /config,
/internal/active-requests— no pasan por el. Vetar `master` en el hook deja a la
master para lo unico que necesita (panel y administracion) y convierte cualquier
uso futuro de inferencia con ella en un 403 con motivo, en vez de una fila
silenciosa en el panel. El watchdog, ultimo consumidor legitimo, paso ese mismo
dia a sondear el catalogo con 0 tokens.

Que afirma cada test:
  * el veto por si solo (master dentro, cualquier otra key fuera);
  * que `_resolve_key_label` traduce el token interno de la master a "master"
    — si eso se rompe, el veto se vuelve invisible y falla CERRADO-ABIERTO:
    la master volveria a colarse como "unknown";
  * que el gate esta ALAMBRA DO en el hook y es el PRIMERO de la cadena, antes
    del gate abliterado: un veto que se pueda reordenar por detras de otro
    gate no es un veto.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"

WANT_FN = {"_master_inference_denied", "_resolve_key_label", "_auth_field"}


def _hook_module():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    src = cm["data"]["litellm_strip_params.py"]
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in WANT_FN]
    missing = WANT_FN - {n.name for n in keep}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("masterveto")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"),
         mod.__dict__)  # noqa: S102
    return mod


@pytest.fixture(scope="module")
def veto():
    return _hook_module()


# ── 1. el veto ──────────────────────────────────────────────────────────────────

def test_master_queda_denegada_para_cualquier_modelo(veto):
    for model in ("qwen38-flash-next", "tooling", "qwen3.8-flash",
                  "qwen38-flash-next-uncensored", "", None):
        denied, detail = veto._master_inference_denied(model, "master")
        assert denied, f"master pudo pedir {model!r}: el veto desaparecio"
        assert "administracion" in detail["error"]


def test_ninguna_otra_key_pierde_inferencia(veto):
    for label in ("hermes", "opencode-20260630-local", "playground",
                  "dgx-dashboard", "open-webui-v3", "unknown", None):
        denied, _ = veto._master_inference_denied("qwen38-flash-next", label)
        assert not denied, f"{label!r} bloqueada: el veto se paso de lista"


def test_la_master_se_reconoce_por_su_token_interno(veto):
    """`_resolve_key_label` es la unica forma de saber que quien llama es la
    master: no tiene alias, y su dict de auth trae el token literal
    `litellm_proxy_master_key`. Si alguien cambia esa cadena, el veto deja de
    verla y falla ABIERTO — este test es el arnés de ese acoplamiento."""
    d = veto._resolve_key_label({"token": "litellm_proxy_master_key"})
    assert d == "master"
    assert veto._resolve_key_label({"token": "88ce4989dd11..."} ) == "unknown"
    assert veto._resolve_key_label({"key_alias": "hermes"}) == "hermes"


# ── 2. el alambre: esta llamado, y es lo primero ────────────────────────────────

def _pre_call_body():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    tree = ast.parse(cm["data"]["litellm_strip_params.py"])

    def walk(node):
        for child in ast.walk(node):
            if (isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "async_pre_call_hook"):
                yield child

    hooks = list(walk(tree))
    target = next(h for h in hooks
                  if any("_master_inference_denied" in ast.dump(n)
                         for n in h.body))
    return target


def test_el_hook_llama_al_veto_antes_que_a_los_demas_gates():
    """Orden = semantica. Si el veto a la master cayera por detras del gate
    abliterado o del de OpenRouter, un pedido con modelo denegado primero
    daria 403 por el otro motivo y la fila `master` seguiria llegando al
    panel (el 403 tambien se registra). El veto tiene que ser el primer
    gate `_xxx_denied` del cuerpo del hook."""
    body = _pre_call_body()
    src = ast.dump(body)
    assert "_master_inference_denied" in src
    for gate in ("_uncensored_access_denied", "_openrouter_access_denied",
                 "_grok_access_denied"):
        assert src.index("_master_inference_denied") < src.index(gate), (
            f"el veto a la master se reordeno por detras de {gate}")


# ── 3. coherencia con la allowlist abliterada ───────────────────────────────────

def test_master_no_sigue_en_la_allowlist_abliterada():
    """El comentario de UNCENSORED_ALLOWED_KEY_ALIASES decia que master entraba
    "porque es la key con la que se mide desde dentro del cluster". Desde el
    29-09 el watchdog no mide con tokens y el hook veta su inferencia: dejarla
    en la lista seria admitir en una puerta lo que la otra niega."""
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    src = cm["data"]["litellm_strip_params.py"]
    tree = ast.parse(src)
    default = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and getattr(n.targets[0], "id", "") == "UNCENSORED_ALLOWED_KEY_ALIASES"
    )
    alias_default = next(
        n for n in ast.walk(default)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "openclaw-qwen36-prod" in n.value
    )
    labels = {a.strip() for a in alias_default.value.split(",") if a.strip()}
    assert "master" not in labels, (
        "master sigue en la allowlist por defecto de la ruta abliterada")
    assert {"playground", "hermes", "synapse"} <= labels, (
        "la purga se llevo por delante keys que si se usan")
