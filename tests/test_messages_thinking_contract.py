"""Contrato del `thinking` Anthropic en /v1/messages contra los locales.

POR QUE EXISTE (2026-09-21)
---------------------------
Medido: `/v1/messages` con `thinking: {type:"enabled", budget_tokens:N}`
contra `qwen38-flash-next` devolvia CERO bloques thinking (todos los budgets
sondeados: 1024/4096/12000/31999, non-stream y stream), mientras el MISMO
backend sin el campo devuelve el razonamiento bien (16108 chars medidos). El
motor pensaba en vano: 3213 tokens de salida para 254 caracteres visibles.

La causa, leida en la imagen v1.100.0 y reproducida con la libreria exacta:
el bridge de LiteLLM traduce `thinking` a `reasoning_effort` y, si el
proveedor es "openai" y hay thinking activo, reescribe el modelo a
"openai/responses/..." — la peticion acaba en POST /v1/responses del backend
(confirmado en vivo en el log de vLLM), y ahi el razonamiento se pierde en la
traduccion responses->chat->anthropic. El flag
LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES NO guarda esa
reescritura interna: solo gobierna el dispatcher exterior.

El fix vive en el hook porque el hook corre ANTES que el bridge: traduce el
`thinking` al tier que promete el motor y RETIRA el campo del cuerpo. Sin
`thinking` el bridge no traduce ni reescribe, y la peticion sigue la ruta
chat/completions que si devuelve el bloque. Este contrato fija la mitad de
servidor: que despues de `_apply_thinking_tier` un alias local NUNCA conserva
`thinking`, que el tier traducido respeta los umbrales del bridge, y que los
alias cloud (no en THINKING_TIERS) NO se tocan — su ruta actual esta medida
funcionando y cambiarla seria romper lo que va bien.

La mitad de extremo a extremo (bloque thinking real por /v1/messages y
`/v1/chat/completions` en el log de vLLM, no `/v1/responses`) se mide contra
el proxy vivo; el resultado viaja en el PR.
"""
import ast
import logging
import sys
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parent.parent / "k8s" / "manifest.yaml"

WANT_FN = {"_apply_thinking_tier", "_client_thinking_tier",
           "_reasoning_effort_value", "_anthropic_thinking_tier",
           "_family_of_alias", "_is_structured_output", "_has_tools"}
WANT_CONST = {"THINKING_TIERS", "THINKING_KWARGS", "CLIENT_EFFORT_TIERS",
              "FAMILY_SAMPLING"}

SCHEMA = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}


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
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(d["data"]["litellm_strip_params.py"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("messages_thinking_pure")
    mod.log = logging.getLogger("test.hook")
    mod.sampling_log = logging.getLogger("test.hook.sampling")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


def _run(hook, body, alias, backend="openai/qwen38-flash-next"):
    _install_fake_litellm(backend)
    data = dict(body)
    hook._apply_thinking_tier(data, alias)
    return data


def _ctk(data):
    return (data.get("extra_body") or {}).get("chat_template_kwargs") or {}


THINKING = {"type": "enabled", "budget_tokens": 12000}


def test_pensamiento_exPLICITO_se_traduce_y_no_viaja(hook):
    """El caso del bug: residente + thinking -> sin `thinking`, con ctk low.

    budget 12000 >= 4096 -> "high" (umbral del bridge), y la tabla qwen traduce
    high->xhigh: el residente VIVO rechaza `high` crudo (400 "Unexpected
    reasoning effort high", medido 22-09 contra el head; chat_template.jinja:48
    valida ('xhigh','medium','low')). `xhigh` es su techo.
    """
    data = _run(hook, {"model": "qwen38-flash-next", "thinking": dict(THINKING)},
                "qwen38-flash-next")
    assert "thinking" not in data, "el thinking viajo crudo: el bridge reescribe a /v1/responses"
    assert _ctk(data) == {"enable_thinking": True, "reasoning_effort": "xhigh"}


@pytest.mark.parametrize("budget,tier", [
    (1024, "low"),      # >= LOW(1024)
    (2048, "medium"),   # >= MEDIUM(2048)
    (4096, "high"),     # >= HIGH(4096) -> high, que la tabla traduce a xhigh
    (512, "low"),       # "minimal" para el bridge; aqui se sube al minimo real
])
def test_umbrales_del_budget_coinciden_con_los_del_bridge(hook, budget, tier):
    # El tier del bridge se traduce por THINKING_KWARGS["qwen"]: high->xhigh
    # (el residente rechaza `high` crudo; ver test_pensamiento_exPLICITO...).
    esperado = {"low": "low", "medium": "medium", "high": "xhigh"}[tier]
    data = _run(hook, {"model": "qwen38-flash-next",
                       "thinking": {"type": "enabled", "budget_tokens": budget}},
                "qwen38-flash-next")
    assert "thinking" not in data
    assert _ctk(data).get("reasoning_effort") == esperado
    assert _ctk(data).get("enable_thinking") is True


def test_disabled_apaga_y_tambien_se_retira(hook):
    data = _run(hook, {"model": "qwen38-flash-next",
                       "thinking": {"type": "disabled"}}, "qwen38-flash-next")
    assert "thinking" not in data
    assert _ctk(data) == {"enable_thinking": False}


def test_adaptive_no_viaja(hook):
    """Claude Code 4.5+ manda `adaptive`: el campo tampoco puede viajar."""
    data = _run(hook, {"model": "qwen38-flash-next",
                       "thinking": {"type": "adaptive"}}, "qwen38-flash-next")
    assert "thinking" not in data
    assert _ctk(data).get("enable_thinking") is True


def test_sin_thinking_no_cambia_nada(hook):
    """Regresion del camino que ya funcionaba: alias solo, default low."""
    data = _run(hook, {"model": "qwen38-flash-next"}, "qwen38-flash-next")
    assert "thinking" not in data
    assert _ctk(data) == {"enable_thinking": True, "reasoning_effort": "low"}


def test_effort_del_cliente_gana_pero_thinking_se_retira_igual(hook):
    """Si un bicho manda effort Y thinking: manda el effort, y thinking NO
    puede quedarse porque su sola presencia dispara la reescritura del bridge."""
    data = _run(hook, {"model": "qwen38-flash-next",
                       "reasoning_effort": "medium",
                       "thinking": {"type": "enabled", "budget_tokens": 12000}},
                "qwen38-flash-next")
    assert "thinking" not in data
    assert "reasoning_effort" not in data        # el effort se tradujo, no viaja crudo
    assert _ctk(data).get("reasoning_effort") == "medium"


@pytest.mark.parametrize("esfuerzo", ["minimal", "auto", "ULTRA", "maximo", ""])
def test_esfuerzo_no_reconocido_no_viaja_crudo(hook, esfuerzo):
    """Clamp total (23-09-2026): un `reasoning_effort` que NO esta en
    CLIENT_EFFORT_TIERS -- vocabulario de otro proveedor (`minimal` de OpenAI),
    `auto`, un typo o el empty string -- cae al default del alias (un nivel
    VALIDO) en chat_template_kwargs, y el valor crudo del cliente NO puede
    viajar: si lo hiciera, el residente lo rechaza con 400 igual que al `high`
    viejo (chat_template.jinja:48 valida solo xhigh/medium/low).

    Es la otra puerta del mismo bug que arregló #125: `high` se colaba por ESTAR
    en la tabla y no traducirse; `minimal` se cuela por NO estar y no retirarse.
    La puerta vieja (`if requested_tier:`) solo cerraba la primera."""
    data = _run(hook, {"model": "qwen38-flash-next", "reasoning_effort": esfuerzo},
                "qwen38-flash-next")
    assert "reasoning_effort" not in data, "el esfuerzo crudo viaja: 400 del residente"
    assert _ctk(data).get("reasoning_effort") in ("low", "medium", "xhigh")
    assert _ctk(data).get("enable_thinking") is True


def test_esfuerzo_anidado_no_reconocido_no_viaja_crudo(hook):
    """Misma garantia para el portador anidado `reasoning: {effort: ...}`."""
    data = _run(hook, {"model": "qwen38-flash-next",
                       "reasoning": {"effort": "minimal"}}, "qwen38-flash-next")
    assert "reasoning" not in data, "el reasoning.effort crudo viaja: 400 del residente"
    assert _ctk(data).get("reasoning_effort") in ("low", "medium", "xhigh")


def test_salida_estructurada_gana_y_thinking_se_retira(hook):
    data = _run(hook, {"model": "qwen38-flash-next",
                       "thinking": dict(THINKING),
                       "response_format": dict(SCHEMA)}, "qwen38-flash-next")
    assert "thinking" not in data
    assert _ctk(data) == {"enable_thinking": False}


def test_alias_cloud_no_se_toca(hook):
    """`alibaba-q38-flash` no esta en THINKING_TIERS y su ruta con `thinking`
    esta medida funcionando hoy (su /responses conserva el razonamiento). El
    fix NO debe cambiarla: ni pop, ni inyeccion."""
    data = _run(hook, {"model": "alibaba-q38-flash", "thinking": dict(THINKING)},
                "alibaba-q38-flash", backend="openai/qwen3.8-flash")
    assert data.get("thinking") == THINKING
    assert not _ctk(data)


def test_sticky_a_cloud_no_se_toca(hook):
    """El session_router puede haber reescrito a Alibaba ANTES de este punto:
    la puerta del pop mira el alias RESUELTO (data['model']), no el pedido.
    El `thinking` NO se retira (su ruta cloud funciona hoy) y el default del
    alias pedido sigue inyectando ctk como siempre — comportamiento previo,
    aqui solo se fija que el pop no se dispare."""
    data = _run(hook, {"model": "alibaba-q38-flash", "thinking": dict(THINKING)},
                "qwen38-flash-next", backend="openai/qwen3.8-flash")
    assert data.get("thinking") == THINKING


def test_cliente_con_ctk_propio_manda_pero_no_deja_thinking(hook):
    """Quien ya opino en chat_template_kwargs conserva su valor; aun asi el
    `thinking` se retira (es la llave de la reescritura, no del nivel)."""
    data = _run(hook, {"model": "qwen38-flash-next",
                       "thinking": dict(THINKING),
                       "extra_body": {"chat_template_kwargs":
                                      {"enable_thinking": True,
                                       "reasoning_effort": "medium"}}},
                "qwen38-flash-next")
    assert "thinking" not in data
    assert _ctk(data) == {"enable_thinking": True, "reasoning_effort": "medium"}


@pytest.mark.parametrize("alias", ["qwen38-off", "qwen38-u-off"])
@pytest.mark.parametrize("body", [
    {"reasoning_effort": "high"},
    {"reasoning_effort": "xhigh"},
    {"reasoning_effort": "low"},
    {"reasoning_effort": "minimal"},
    {"extra_body": {"reasoning_effort": "max"}},
    {"thinking": {"type": "enabled", "budget_tokens": 12000}},
])
def test_alias_apagado_no_piensa_aunque_el_cliente_lo_pida(hook, alias, body):
    """23-09-2026: `qwen38-off`/`qwen38-u-off` son el residente SIN pensar, y
    ningun effort del cliente los enciende: se traduce a off y el crudo no viaja.
    El hook reescribe data["model"] al residente; el alias pedido llega aparte."""
    data = _run(hook, {"model": "qwen38-flash-next", **{k: (dict(v) if isinstance(v, dict) else v)
                                                        for k, v in body.items()}}, alias)
    assert _ctk(data) == {"enable_thinking": False}
    assert "reasoning_effort" not in data
    assert "reasoning_effort" not in (data.get("extra_body") or {})
    assert "thinking" not in data


def test_alias_que_piensa_sigue_honrando_el_effort_del_cliente(hook):
    """La regla general no cambia: en un alias que piensa, el effort del cliente gana."""
    data = _run(hook, {"model": "qwen38-flash-next", "reasoning_effort": "high"},
                "qwen38-flash-next")
    assert _ctk(data) == {"enable_thinking": True, "reasoning_effort": "xhigh"}
