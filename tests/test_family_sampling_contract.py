"""Contrato del sampling por FAMILIA del backend.

Los alias de capacidad (`tooling`, y hasta el 24-08-2026 tambien `high` y `max`,
hoy retirados en favor de `reasoning_effort`) los sirve el residente que este
el residente del momento, y cada familia de checkpoint quiere un sampling
distinto. El cliente no puede saberlo, asi que lo pone el hook.

Lo que este fichero fija es la propiedad que se rompio de verdad: el perfil se
elige por el backend que VA A ATENDER la peticion, no por el nombre que escribio
el cliente. Sin esa resolucion coincidirian solo por casualidad segun el perfil de
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
           "_is_structured_output", "_disable_thinking",
           "_apply_thinking_tier",
           # 2026-08-14: `_apply_thinking_tier` mira primero el `reasoning_effort`
           # del cliente. Sin estos dos el except de la funcion se traga un
           # NameError y el tier deja de aplicarse EN SILENCIO.
           "_client_thinking_tier", "_reasoning_effort_value"}
WANT_CONST = {"FAMILY_SAMPLING", "SWAPPABLE_ALIASES",
              "THINKING_TIERS", "THINKING_KWARGS", "CLIENT_EFFORT_TIERS"}


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


# ── Nivel de pensamiento por nombre de alias ────────────────────────────────
#
# Tres alias sobre el MISMO backend que solo se diferencian en cuanto piensan.
# Existen para el cliente que solo tiene un campo `model` en un YAML -- k8sgpt,
# Aurora, los ~15 adapters de Synapse -- y que no puede mandar extra_body.


def _ctk(data):
    return (data.get("extra_body") or {}).get("chat_template_kwargs") or {}


def test_cada_nivel_pedido_se_traduce_al_dialecto_de_deepseek(hook):
    """El nivel se pide con `reasoning_effort` y se traduce al dialecto de la
    familia viva. DeepSeek lee `thinking` + `reasoning_effort`.

    24-08-2026: hasta hoy el nivel tambien podia venir en el NOMBRE (`high`,
    `max`). Esos dos alias se retiran del model_list —eran el mismo backend con
    otro nivel— y el unico nombre que sigue diciendo algo del pensamiento es
    `tooling`, que significa «no pienses».
    """
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = {"model": "tooling"}
    hook._apply_thinking_tier(data, "tooling")
    assert _ctk(data) == {"thinking": False}

    esperado = {
        "high": {"thinking": True, "reasoning_effort": "high"},
        "max": {"thinking": True, "reasoning_effort": "max"},
    }
    for effort, kwargs in esperado.items():
        data = {"model": "tooling", "reasoning_effort": effort}
        hook._apply_thinking_tier(data, "tooling")
        assert _ctk(data) == kwargs, effort


def test_qwen_no_gradua_pero_si_enciende_y_apaga(hook):
    """Qwen no tiene reasoning_effort: solo `enable_thinking`. Los niveles
    colapsan a "piensa", y `tooling` sigue significando "no pienses". Lo que NO
    puede pasar es que se le cuelen las claves de DeepSeek."""
    _install_fake_litellm({"tooling": _dep("openai/nvidia-qwen36-35b-nvfp4")})
    for effort in ("high", "max"):
        data = {"model": "tooling", "reasoning_effort": effort}
        hook._apply_thinking_tier(data, "tooling")
        assert _ctk(data) == {"enable_thinking": True}, effort
    data = {"model": "tooling"}
    hook._apply_thinking_tier(data, "tooling")
    assert _ctk(data) == {"enable_thinking": False}


def test_el_tier_sale_de_lo_PEDIDO_no_del_modelo_resuelto(hook):
    """La propiedad que justifica calcular el tier antes de resolver el destino.

    Cuando la peticion degrada a otro alias, data["model"] ya dice otra cosa. Si
    el tier se calculara sobre el resuelto, un `max` degradado se quedaria sin
    pensar, que es justo el nivel contrario al que se pidio.
    """
    _install_fake_litellm({"dense": _dep("openai/deepseek-v4-flash-0731")})
    data = {"model": "dense", "reasoning_effort": "max"}
    hook._apply_thinking_tier(data, "tooling")
    assert _ctk(data) == {"thinking": True, "reasoning_effort": "max"}


def test_un_nombre_desconocido_no_tiene_tier_propio(hook):
    """La funcion pura no inventa un tier para un nombre desconocido."""
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    for alias in ("dense", "desconocido", ""):
        data = {"model": "tooling"}
        hook._apply_thinking_tier(data, alias)
        assert "extra_body" not in data, alias


def test_el_cliente_explicito_gana_al_alias(hook):
    """Quien sabe mandar extra_body no necesita que le elijan el nivel: es lo que
    hacen los perfiles del playground. Si el alias ganara, no habria forma de
    probar `max` contra un alias que no se llame `max`."""
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = {"model": "tooling",
            "extra_body": {"chat_template_kwargs": {"thinking": True,
                                                    "reasoning_effort": "max"}}}
    hook._apply_thinking_tier(data, "tooling")   # el alias diria thinking:False
    assert _ctk(data) == {"thinking": True, "reasoning_effort": "max"}


def test_la_salida_estructurada_apaga_el_pensamiento_pida_lo_que_pida_el_alias(hook):
    """Medido el 2026-08-10: con thinking activo se cuela una llave suelta del
    razonamiento delante del JSON guiado y el parse revienta (3/3). Pedir `max` y
    un json_schema a la vez no es mas pensamiento, es una contradiccion."""
    _install_fake_litellm({"tooling": _dep("openai/deepseek-v4-flash-0731")})
    data = {
        "model": "tooling",
        "reasoning_effort": "max",
        "response_format": {"type": "json_schema"},
    }
    hook._apply_thinking_tier(data, "tooling")
    assert _ctk(data) == {"thinking": False}


def test_los_tres_tiers_son_swappable(hook):
    """Si un tier se quedara fuera de SWAPPABLE_ALIASES, _apply_family_sampling
    saldria antes de tiempo y ese alias mandaria a DeepSeek el sampling de Qwen."""
    for alias in hook.THINKING_TIERS:
        assert alias in hook.SWAPPABLE_ALIASES, alias


def test_ningun_tier_dice_medium(hook):
    """El tokenizer viene parcheado para que lo desconocido caiga a "low", no a
    "high". Un tier "medium" se comportaria como low y el nombre mentiria."""
    for fam, niveles in hook.THINKING_KWARGS.items():
        assert "medium" not in niveles, fam
        for kw in niveles.values():
            assert kw.get("reasoning_effort") in (None, "low", "high", "max")
