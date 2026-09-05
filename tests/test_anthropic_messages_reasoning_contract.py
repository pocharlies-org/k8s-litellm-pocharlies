"""Contrato de `/v1/messages` (formato Anthropic) con el razonamiento local.

POR QUE EXISTE (2026-09-05, SC-203)
-----------------------------------
El objetivo del epic es que el thinking de `qwen38-flash-next`/`tooling`
funcione de punta a punta para un cliente Claude Code, y con `low` por defecto.
Eso tiene dos mitades, y las dos son contratos de este manifiesto:

1. El flag que hace que el razonamiento salga por el canal `thinking` de
   Anthropic: `LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true`
   en el contenedor PRINCIPAL del Deployment `litellm`. Sin el, /v1/messages
   va por la ruta de responses y el razonamiento traducido por el hook no
   llega como bloques thinking. Documentado en `litellm/__init__.py` de la
   libreria (imagen v1.96.0). OJO: es GLOBAL — cambia la ruta de /v1/messages
   para TODOS los modelos del proxy (los residentes de otras companies,
   SC-188/SC-189/SC-196, consumen ese endpoint).

2. D7 (criterio exacto de pm/SC-205, comentario Jira id 10543, citado textual):
   una petición `/v1/messages` sin campo `thinking` y con `max_tokens` pequeño
   (p. ej. 512) DEBE devolver `content` no vacío. Es el guardarraíl para que el
   default nuevo `low` no produzca respuestas vacías cuando el cliente pide
   pocos tokens.

   La aserción mecánica de aquí es la MITAD DE SERVIDOR de ese criterio: con
   /v1/messages sin `thinking`, lo que este manifiesto le garantiza al motor es
   (a) que el default aplicado sea `low` — el nivel acotado, no el xhigh de
   fábrica, que con 512 tokens agota el presupuesto pensando y devuelve
   content VACIO con finish=length (medido 31-08: sin effort, reasoning 17658
   chars, content 0) — y (b) que ningún guard de tools apague el tier. La
   mitad de extremo a extremo (la petición real y el content no vacío) se mide
   contra el proxy vivo; el resultado de esa medición viaja en el PR.
"""
import ast
import logging
import sys
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_apply_thinking_tier", "_client_thinking_tier",
           "_reasoning_effort_value", "_family_of_alias",
           "_is_structured_output", "_has_tools"}
WANT_CONST = {"THINKING_TIERS", "THINKING_KWARGS", "CLIENT_EFFORT_TIERS",
              "FAMILY_SAMPLING"}


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _deployment(nombre):
    for doc in _docs():
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == nombre:
            return doc
    raise AssertionError(f"no encuentro el Deployment {nombre}")


def _container(deployment, nombre):
    for c in deployment["spec"]["template"]["spec"]["containers"]:
        if c["name"] == nombre:
            return c
    raise AssertionError(f"no encuentro el contenedor {nombre}")


def _install_fake_litellm(model):
    class FakeRouter:
        def get_model_list(self, model_name=None):
            return [{"litellm_params": {"model": model}}]

    proxy_server = types.ModuleType("litellm.proxy.proxy_server")
    proxy_server.llm_router = FakeRouter()
    proxy = types.ModuleType("litellm.proxy")
    proxy.proxy_server = proxy_server
    litellm = types.ModuleType("litellm")
    litellm.proxy = proxy
    sys.modules["litellm"] = litellm
    sys.modules["litellm.proxy"] = proxy
    sys.modules["litellm.proxy.proxy_server"] = proxy_server


@pytest.fixture(scope="module")
def hook():
    src = next(d["data"]["litellm_strip_params.py"] for d in _docs()
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))]
    mod = types.ModuleType("messagespure")
    mod.log = logging.getLogger("test.hook")
    mod.sampling_log = logging.getLogger("test.hook.sampling")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


# ── 1. El flag D6, en el sitio exacto ─────────────────────────────────────────


def test_el_flag_de_ruta_de_chat_completions_esta_en_el_contenedor_principal():
    env = {e["name"]: e.get("value")
           for e in _container(_deployment("litellm"), "litellm")["env"]}
    assert env.get("LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES") == "true"


def test_el_flag_NO_esta_en_el_sidecar():
    """El sidecar `active-requests-api` es otro contenedor con su propio env.
    Ponerlo ahi no cambia ninguna ruta y siembra la duda de donde vive."""
    env = {e["name"] for e in _container(_deployment("litellm"),
                                         "active-requests-api")["env"]}
    assert "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES" not in env


# ── 2. D7: /v1/messages sin `thinking`, max_tokens pequeno ───────────────────


def _ctk(hook, data, alias):
    _install_fake_litellm("openai/qwen38-flash-next")
    hook._apply_thinking_tier(data, alias)
    return (data.get("extra_body") or {}).get("chat_template_kwargs") or {}


@pytest.mark.parametrize("alias", ["qwen38-flash-next", "tooling"])
def test_una_peticion_anthropic_sin_thinking_sale_con_el_nivel_acotado(hook, alias):
    """/v1/messages sin campo `thinking` y con max_tokens=512 (D7, pm/SC-205,
    comentario Jira 10543): lo que el manifiesto garantiza es que el nivel que
    sale es `low` — acotado y con content — y no el xhigh de fabrica, que con
    512 tokens devuelve content VACIO con finish=length."""
    data = {"model": alias, "max_tokens": 512,
            "messages": [{"role": "user", "content": "hola"}]}
    assert _ctk(hook, data, alias) == {"enable_thinking": True,
                                       "reasoning_effort": "low"}


@pytest.mark.parametrize("alias", ["qwen38-flash-next", "tooling"])
def test_los_dos_alias_del_objetivo_arrancan_en_low(hook, alias):
    """El default efectivo es `low` por la via del alias (THINKING_TIERS), no
    por accidente de un effort que el cliente no mando."""
    assert hook.THINKING_TIERS[alias] == "low"


def test_el_guard_de_tools_ya_no_apaga_el_pensamiento(hook):
    """D4: el guard sglang#36537 se retira (la reproduccion de QA del 05-09 no
    reprodujo el bucle de token-id-0 con tools + thinking). Un /v1/messages de
    Claude Code trae tools en casi todos los turnos: si el guard siguiera, el
    razonamiento del cliente Anthropic estaria muerto por esa puerta aunque el
    default fuera low."""
    data = {"model": "tooling", "max_tokens": 512,
            "tools": [{"type": "function",
                       "function": {"name": "read", "parameters": {}}}]}
    assert _ctk(hook, data, "tooling") == {"enable_thinking": True,
                                           "reasoning_effort": "low"}


def test_el_thinking_de_anthropic_traducido_por_litellm_no_se_pierde(hook):
    """Con D6, LiteLLM traduce el bloque `thinking` de Anthropic a
    `reasoning_effort` ANTES de que corra el hook: `low` llega como "low" y un
    budget numerico como "budget:<tokens>". Los dos tienen que traducir a un
    nivel de la tabla — si cayeran fuera, decidiria el alias y el esfuerzo del
    cliente se perderia en la ruta Anthropic."""
    assert hook._reasoning_effort_value({"reasoning_effort": "budget:2048"}) == "medium"
    assert hook._reasoning_effort_value({"reasoning_effort": "budget:512"}) == "low"
    assert hook._reasoning_effort_value({"reasoning_effort": "budget:20000"}) == "max"
    # Y el valor resultante SI esta en la tabla del cliente: es una orden.
    for presupuesto in ("512", "2048", "20000"):
        tier = hook.CLIENT_EFFORT_TIERS.get(
            hook._reasoning_effort_value({"reasoning_effort": f"budget:{presupuesto}"}))
        assert tier in hook.THINKING_KWARGS["qwen"], presupuesto
    # Un budget que no es numero no se adivina: cae fuera, como siempre.
    assert hook._reasoning_effort_value({"reasoning_effort": "budget:abc"}) == ""
