"""Contrato del sampling por FAMILIA del backend.

Los alias compartidos (`tooling`, `dense`, `litellmrouter`...) los sirve quien sea
el residente del momento, y cada familia de checkpoint quiere un sampling
distinto. El cliente no puede saberlo, asi que lo pone el hook.

Lo que este fichero fija es la propiedad que se rompio de verdad: el perfil se
elige por el backend que VA A ATENDER la peticion, no por el nombre que escribio
el cliente. Con `litellmrouter` coinciden solo por casualidad segun el perfil de
residente que este puesto, y con cualquier alias que el hook reescriba no coinciden.

Se carga el hook REAL del manifest y se ejecutan solo sus funciones puras, con un
llm_router de mentira, igual que el resto de los contratos de este repo.
"""
import ast
import sys
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_apply_family_sampling", "_family_of_alias",
           "_is_structured_output", "_disable_thinking"}
WANT_CONST = {"FAMILY_SAMPLING", "SWAPPABLE_ALIASES"}


def _install_fake_litellm(deployments):
    """Inyecta un `litellm.proxy.proxy_server.llm_router` de mentira.

    `_family_of_alias` lo importa DENTRO de la funcion (import diferido), asi que
    basta con dejarlo en sys.modules antes de llamarla.
    """
    class FakeRouter:
        def get_model_list(self, model_name=None):
            return deployments.get(model_name) or []

    proxy_server = types.ModuleType("litellm.proxy.proxy_server")
    proxy_server.llm_router = FakeRouter()
    proxy = types.ModuleType("litellm.proxy")
    proxy.proxy_server = proxy_server
    litellm = types.ModuleType("litellm")
    litellm.proxy = proxy
    sys.modules["litellm"] = litellm
    sys.modules["litellm.proxy"] = proxy
    sys.modules["litellm.proxy.proxy_server"] = proxy_server


def _dep(model):
    return [{"litellm_params": {"model": model}}]


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
    mod = types.ModuleType("samplingpure")
    # Los dos usan `log`/`sampling_log` para no romper nunca una peticion.
    import logging
    mod.log = logging.getLogger("test.hook")
    mod.sampling_log = logging.getLogger("test.hook.sampling")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


def test_deepseek_pisa_el_sampling_de_qwen(hook):
    """Con DeepSeek detras de `tooling`, los valores de Qwen se van.

    presence_penalty 1.5 es el que trae OpenClaw hardcodeado por alias en ~8
    agentes: a DeepSeek le alarga las respuestas y le hace divagar. top_k/min_p no
    estan documentados en su model card.
    """
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = {"model": "tooling", "temperature": 0.7, "top_p": 0.8,
            "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}
    hook._apply_family_sampling(data)
    assert data["temperature"] == 1.0 and data["top_p"] == 0.95
    for gone in ("top_k", "min_p", "presence_penalty"):
        assert gone not in data, f"{gone} llego a DeepSeek"


def test_qwen_mantiene_su_perfil_en_el_mismo_alias(hook):
    """Mismo alias, otro residente, otro perfil. Es lo que hace que el boton de
    residente GPU no obligue a tocar ningun cliente."""
    _install_fake_litellm({"tooling": _dep("openai/nvidia-qwen36-35b-nvfp4")})
    data = {"model": "tooling", "temperature": 0.05}
    hook._apply_family_sampling(data)
    # 0.7, no 0: Qwen documenta que el greedy decoding degrada el tool-calling.
    assert data["temperature"] == 0.7 and data["top_p"] == 0.8


def test_el_perfil_se_elige_por_el_alias_RESUELTO_no_por_el_que_pidio_el_cliente(hook):
    """La regresion concreta que arreglo el movimiento de la llamada (2026-08-10).

    Un alias que el hook reescribe puede no estar en SWAPPABLE_ALIASES: si
    `_apply_family_sampling` corre ANTES de resolverlo, sale por el return temprano
    y no se aplica ningun perfil; corriendo DESPUES ve el alias real y aplica el del
    residente. Se usa un nombre inexistente como sonda porque el sintoma es
    exactamente ese: alias no reconocido -> sin perfil.
    """
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})

    sin_resolver = {"model": "alias-que-el-hook-reescribe", "temperature": 0.7,
                    "presence_penalty": 1.5}
    hook._apply_family_sampling(sin_resolver)
    assert sin_resolver["temperature"] == 0.7, "sin resolver no debe llevar perfil"
    assert "presence_penalty" in sin_resolver

    ya_resuelto = {"model": "tooling", "temperature": 0.7, "presence_penalty": 1.5}
    hook._apply_family_sampling(ya_resuelto)
    assert ya_resuelto["temperature"] == 1.0
    assert "presence_penalty" not in ya_resuelto


def test_un_alias_que_nombra_un_modelo_concreto_no_se_toca(hook):
    """`deepseek-v4-flash-opencode` y compania ya llevan su sampling en el
    model_list: quien los pide sabe con que habla y no se le pisa."""
    _install_fake_litellm({
        "deepseek-v4-flash-opencode": _dep("openai/deepseek-v4-flash-0731")})
    data = {"model": "deepseek-v4-flash-opencode", "temperature": 0.2}
    hook._apply_family_sampling(data)
    assert data["temperature"] == 0.2
    assert "deepseek-v4-flash-opencode" not in hook.SWAPPABLE_ALIASES


def test_sin_backend_registrado_no_se_inventa_perfil(hook):
    """Mejor no aplicar perfil que aplicar uno equivocado: si el alias no tiene
    deployment (residente a medio cambiar), se deja la peticion como vino."""
    _install_fake_litellm({})
    data = {"model": "tooling", "temperature": 0.3, "presence_penalty": 1.5}
    hook._apply_family_sampling(data)
    assert data == {"model": "tooling", "temperature": 0.3, "presence_penalty": 1.5}


# ── Salida estructurada ────────────────────────────────────────────────────────
# Un bug de datos, no de estilo: con `thinking` activo (el servidor arranca con
# --default-chat-template-kwargs thinking=True) una peticion con `response_format`
# devuelve el JSON CORRUPTO -- se cuela un `{` suelto del razonamiento delante del
# JSON guiado. Reproducido 3/3 el 2026-08-10; con thinking off, 2/2 correcto.
#
# Quien lo sufria: la extraccion fiscal de skirmbooks, que ante el JSON.parse
# fallido cae a heuristicas de regex dentro de un `catch` que solo escribe un
# console.warn. Llevaba degradando en silencio desde el corte a DeepSeek.

def _structured(**extra):
    d = {"model": "tooling",
         "response_format": {"type": "json_schema",
                             "json_schema": {"name": "X", "strict": True, "schema": {}}}}
    d.update(extra)
    return d


def test_estructurada_contra_deepseek_apaga_el_pensamiento(hook):
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = _structured()
    hook._apply_family_sampling(data)
    assert data["extra_body"]["chat_template_kwargs"]["thinking"] is False


def test_estructurada_respeta_la_temperatura_del_cliente(hook):
    """El esquema fija la FORMA, no los VALORES: a temperatura 1.0 los numeros
    bailan, y quien pide un esquema quiere un dato. Si mando 0, se respeta."""
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = _structured(temperature=0)
    hook._apply_family_sampling(data)
    assert data["temperature"] == 0, "el perfil de familia no debe subirla a 1.0"


def test_estructurada_sigue_quitando_los_penalties_de_qwen(hook):
    """Los `drop` no dependen de que haya esquema: a DeepSeek le sientan mal igual."""
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = _structured(temperature=0, presence_penalty=1.5, top_k=20)
    hook._apply_family_sampling(data)
    assert "presence_penalty" not in data and "top_k" not in data


def test_sin_esquema_el_perfil_de_familia_manda_como_siempre(hook):
    """La ruta normal no cambia: sin `response_format` se sigue aplicando el
    sampling agentico que recomienda el model card, y no se toca el pensamiento."""
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = {"model": "tooling", "temperature": 0}
    hook._apply_family_sampling(data)
    assert data["temperature"] == 1.0 and data["top_p"] == 0.95
    assert "extra_body" not in data


def test_estructurada_contra_qwen_no_toca_el_pensamiento(hook):
    """`thinking` es un chat_template_kwarg de DeepSeek. Con un residente Qwen no
    se inventa el parametro: mandarlo a un backend que no lo conoce es basura."""
    _install_fake_litellm({"tooling": _dep("openai/nvidia-qwen36-35b-nvfp4")})
    data = _structured(temperature=0)
    hook._apply_family_sampling(data)
    assert "extra_body" not in data
    assert data["temperature"] == 0


def test_detector_de_salida_estructurada(hook):
    assert hook._is_structured_output({"response_format": {"type": "json_schema"}})
    assert hook._is_structured_output({"response_format": {"type": "json_object"}})
    assert not hook._is_structured_output({"response_format": {"type": "text"}})
    assert not hook._is_structured_output({})
