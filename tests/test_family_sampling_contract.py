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
           "_client_thinking_tier", "_reasoning_effort_value",
           # 2026-08-28: mismo motivo que los dos de arriba. `_thinking_is_on`
           # lo llama `_apply_family_sampling`; si no se exporta aqui, el
           # NameError cae en su except y el perfil de familia deja de
           # aplicarse ENTERO, no solo la parte nueva.
           "_thinking_is_on", "_has_tools"}
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
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})

    # 01-09-2026: el discriminador ya no puede ser `temperature`. Con DeepSeek
    # retirado la unica familia es `qwen`, cuyo perfil la pone en 0.7 -- el mismo
    # valor con el que llega la sonda, asi que no distinguiria "sin perfil" de
    # "con perfil". Se usa `top_p`, que SOLO aparece si el perfil se aplica.
    # `presence_penalty` tampoco sirve ya: el `drop` de qwen esta vacio.
    sin_resolver = {"model": "alias-que-el-hook-reescribe", "temperature": 0.7}
    hook._apply_family_sampling(sin_resolver)
    assert "top_p" not in sin_resolver, "sin resolver no debe llevar perfil"

    ya_resuelto = {"model": "tooling", "temperature": 0.7}
    hook._apply_family_sampling(ya_resuelto)
    assert ya_resuelto["top_p"] == 0.8
    assert ya_resuelto["top_k"] == 20 and ya_resuelto["min_p"] == 0.0


def test_un_alias_que_nombra_un_modelo_concreto_no_se_toca(hook):
    """`deepseek-v4-flash-opencode` y compania ya llevan su sampling en el
    model_list: quien los pide sabe con que habla y no se le pisa."""
    _install_fake_litellm({
        "deepseek-v4-flash-opencode": _dep("openai/qwen38-flash-next")})
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
def test_estructurada_respeta_la_temperatura_del_cliente(hook):
    """El esquema fija la FORMA, no los VALORES: a temperatura 1.0 los numeros
    bailan, y quien pide un esquema quiere un dato. Si mando 0, se respeta."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = _structured(temperature=0)
    hook._apply_family_sampling(data)
    assert data["temperature"] == 0, "el perfil de familia no debe subirla a 1.0"


def test_estructurada_respeta_el_sampling_del_cliente(hook):
    """Con esquema manda el cliente, no el perfil de familia.

    01-09-2026: antes esto fijaba que los `drop` se aplicaban igual con esquema,
    usando a DeepSeek de sujeto. Al retirarlo la unica familia es `qwen`, y su
    `drop` esta VACIO: no hay nada que quitar. Lo que si sigue siendo invariante
    -- y es lo que se fija aqui -- es que con salida estructurada el perfil NO
    pisa lo que mande el cliente.
    """
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = _structured(temperature=0, presence_penalty=1.5, top_k=20)
    hook._apply_family_sampling(data)
    assert data["temperature"] == 0, "con esquema no se pisa la temperatura"
    assert data["presence_penalty"] == 1.5 and data["top_k"] == 20


def test_sin_esquema_el_perfil_de_familia_manda_como_siempre(hook):
    """La ruta normal no cambia: sin `response_format` se sigue aplicando el
    sampling agentico que recomienda el model card, y no se toca el pensamiento."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = {"model": "tooling", "temperature": 0}
    hook._apply_family_sampling(data)
    # Perfil de la familia `qwen`, la unica que queda desde el 01-09-2026.
    assert data["temperature"] == 0.7 and data["top_p"] == 0.8
    assert data["top_k"] == 20 and data["min_p"] == 0.0
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
def test_qwen_no_gradua_pero_si_enciende_y_apaga(hook):
    """Qwen enciende y apaga, y ademas ACOTA el nivel. Lo que NO puede pasar es
    que se le cuelen las claves de DeepSeek (`thinking`, effort `high`/`max`).

    Actualizado 31-08-2026: qwen38-flash-next SI gradua. Su chat template hace `reasoning_effort|default('xhigh')` y valida ('xhigh','medium','low'), asi que no traducir dejaba TODO en el maximo. `medium` cae en una rama elif de la plantilla sin `reasoning_instructions` propio (hecho de la plantilla, sigue en pie). CORREGIDO 05-09-2026 (SC-203): el corolario "medium 2721 vs low 2993, indistinguibles" esta refutado — medido hoy via proxy con el razonamiento encendido, medium da reasoning real por encima de low; `high` mapea a `low` porque el backend no tiene nivel `high` (400), no porque medium sea silencio."""
    _install_fake_litellm({"tooling": _dep("openai/nvidia-qwen36-35b-nvfp4")})
    esperado = {"high": "low", "max": "xhigh", "medium": "medium"}
    for effort, backend_effort in esperado.items():
        data = {"model": "tooling", "reasoning_effort": effort}
        hook._apply_thinking_tier(data, "tooling")
        assert _ctk(data) == {
            "enable_thinking": True,
            "reasoning_effort": backend_effort,
        }, effort
    data = {"model": "tooling"}
    hook._apply_thinking_tier(data, "tooling")
    # 05-09-2026 (SC-203): sin effort el default de `tooling` es `low`, no off.
    assert _ctk(data) == hook.THINKING_KWARGS["qwen"]["low"]


def test_el_tier_sale_de_lo_PEDIDO_no_del_modelo_resuelto(hook):
    """La propiedad que justifica calcular el tier antes de resolver el destino.

    Cuando la peticion degrada a otro alias, data["model"] ya dice otra cosa. Si
    el tier se calculara sobre el resuelto, un `max` degradado se quedaria sin
    pensar, que es justo el nivel contrario al que se pidio.
    """
    _install_fake_litellm({"dense": _dep("openai/qwen38-flash-next")})
    data = {"model": "dense", "reasoning_effort": "max"}
    hook._apply_thinking_tier(data, "tooling")
    # Se deriva de la tabla del hook en vez de fijarla a mano: asi el test
    # sobrevive a que cambie el dialecto de qwen (p.ej. al anadir la
    # traduccion de reasoning_effort) sin dejar de proteger la propiedad.
    assert _ctk(data) == hook.THINKING_KWARGS["qwen"]["max"]


def test_un_nombre_desconocido_no_tiene_tier_propio(hook):
    """La funcion pura no inventa un tier para un nombre desconocido."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    for alias in ("dense", "desconocido", ""):
        data = {"model": "tooling"}
        hook._apply_thinking_tier(data, alias)
        assert "extra_body" not in data, alias


def test_el_cliente_explicito_gana_al_alias(hook):
    """Quien sabe mandar extra_body no necesita que le elijan el nivel: es lo que
    hacen los perfiles del playground. Si el alias ganara, no habria forma de
    probar `max` contra un alias que no se llame `max`."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    # 01-09-2026: el cliente manda en el dialecto del residente vivo (`qwen`
    # usa `enable_thinking`, DeepSeek usaba `thinking`). Lo que se fija es que
    # SU valor sobrevive intacto, no que coincida con la tabla del hook: si
    # coincidiera, el test no distinguiria "gana el cliente" de "gana el alias".
    pedido = {"enable_thinking": True, "reasoning_effort": "xhigh"}
    data = {"model": "tooling",
            "extra_body": {"chat_template_kwargs": dict(pedido)}}
    hook._apply_thinking_tier(data, "tooling")   # el alias diria thinking off
    assert _ctk(data) == pedido


def test_la_salida_estructurada_apaga_el_pensamiento_pida_lo_que_pida_el_alias(hook):
    """Medido el 2026-08-10: con thinking activo se cuela una llave suelta del
    razonamiento delante del JSON guiado y el parse revienta (3/3). Pedir `max` y
    un json_schema a la vez no es mas pensamiento, es una contradiccion."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = {
        "model": "tooling",
        "reasoning_effort": "max",
        "response_format": {"type": "json_schema"},
    }
    hook._apply_thinking_tier(data, "tooling")
    assert _ctk(data) == hook.THINKING_KWARGS["qwen"]["off"]


def test_los_tres_tiers_son_swappable(hook):
    """Si un tier se quedara fuera de SWAPPABLE_ALIASES, _apply_family_sampling
    saldria antes de tiempo y ese alias mandaria a DeepSeek el sampling de Qwen."""
    for alias in hook.THINKING_TIERS:
        assert alias in hook.SWAPPABLE_ALIASES, alias


def test_ningun_tier_manda_un_effort_que_el_backend_no_tiene(hook):
    """Ningun tier MANDA al backend un `reasoning_effort` que ese backend rechaza.

    05-09-2026 (SC-203): este test se LLAMABA `test_ningun_tier_dice_medium` y
    prohibia que existiera un tier `medium`. Esa prohibicion se RETIRA con el
    menu oficial: el backend de qwen38-flash-next SI acepta `medium` — su
    propio 400 lo dice: "Supported types are xhigh (default), medium, and low"
    — y la model card lo lista. Lo que el test protege de verdad, y sigue
    protegiendo, es que ningun valor CRUDO viaje al backend: los unicos efforts
    que salen de la tabla son los que el motor acepta. `high`/`max` como
    valores de backend NO existen (400), y por eso el alias deprecado `high`
    traduce a `low` y `max` a `xhigh`."""
    aceptados_por_el_motor = {None, "low", "medium", "xhigh"}
    for fam, niveles in hook.THINKING_KWARGS.items():
        for kw in niveles.values():
            assert kw.get("reasoning_effort") in aceptados_por_el_motor, fam


# ── La forma REAL de produccion (2026-08-25) ────────────────────────────────
#
# Los tests de arriba llaman con `data["model"] = "tooling"`, o sea con el alias
# SIN reescribir. Produccion no tiene esa forma: `_apply_family_sampling` corre
# despues del salto de CAPABILITY_CHAINS, y con `compute_mode: llm-tp` ese salto
# ya ha puesto `data["model"] = "deepseek-v4-flash-0731"` (TOOLING_MODE_TARGETS).
#
# Por eso el fixture no cazo que la funcion llevaba semanas MUERTA: la puerta
# comparaba el nombre post-reescritura contra un conjunto de nombres
# pre-reescritura. Medido contra el cluster antes del arreglo, con los dos
# alias: `min_p=1.0` daba 1/4 salidas unicas (argmax -> el min_p LLEGO al
# backend) y `temperature=0` daba 1/4 (no se piso a 1.0).
# Qwen documenta DOS perfiles, no uno: 0.7/0.8 sin pensar y 0.6/0.95 pensando,
# con top_k 20 / min_p 0 en los dos. Hasta hoy solo existia el primero, asi que
# el unico caso en que un alias qwen piensa -- `reasoning_effort: high|max` --
# era tambien el unico que recibia el perfil equivocado.
#
# El orden importa y por eso `_thinking_is_on` recalcula en vez de leer:
# `_apply_family_sampling` corre ANTES que `_apply_thinking_tier`, asi que
# cuando el sampling decide no hay chat_template_kwargs escritos todavia.

def test_qwen_sin_pensar_lleva_los_cuatro_del_model_card(hook):
    """top_k y min_p faltaban: no estaban en `set` ni en `drop`, asi que si el
    cliente no los mandaba -- y ninguno los manda -- decodificaba con la cola
    entera de tokens.

    05-09-2026 (SC-203): el default de `tooling` es `low` desde hoy, asi que
    el perfil de no-pensar se sondea pidiendo `none` explicito (era el caso
    "sin effort" antes de ese cambio)."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = {"model": "tooling", "reasoning_effort": "none"}
    hook._apply_family_sampling(data, "tooling")
    assert data["temperature"] == 0.7 and data["top_p"] == 0.8
    assert data["top_k"] == 20 and data["min_p"] == 0.0


def test_qwen_pensando_cambia_al_perfil_de_pensar(hook):
    """La regresion que arregla este commit: `high` encendia el pensamiento en
    THINKING_KWARGS y se quedaba con 0.7/0.8, que es el perfil de NO pensar."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = {"model": "tooling", "reasoning_effort": "high"}
    hook._apply_family_sampling(data, "tooling")
    assert data["temperature"] == 0.6 and data["top_p"] == 0.95
    assert data["top_k"] == 20 and data["min_p"] == 0.0


def test_el_alias_que_no_piensa_no_se_lleva_el_perfil_de_pensar(hook):
    """Un alias con tier "off" no piensa sin effort explicito, y su perfil
    tiene que ser el de no pensar.

    05-09-2026 (SC-203): `qwen38-flash-next` ya NO esta en THINKING_TIERS como
    "off" — su default es `low`. La propiedad que este test fija (tier off ->
    perfil de no pensar) sigue en pie y se sondea con el alias de nombre
    desconocido, que no tiene tier propio."""
    _install_fake_litellm(
        {"qwen38-flash-next": _dep("openai/qwen38-flash-next")})
    data = {"model": "qwen38-flash-next", "reasoning_effort": "none"}
    hook._apply_family_sampling(data, "qwen38-flash-next")
    assert data["temperature"] == 0.7 and data["top_p"] == 0.8


def test_el_cliente_que_manda_extra_body_decide_tambien_el_sampling(hook):
    """`_apply_thinking_tier` no pisa un chat_template_kwargs que ya trae el
    cliente, asi que el sampling tampoco puede ignorarlo: si el cliente
    enciende el pensamiento a mano, el perfil es el de pensar."""
    _install_fake_litellm(
        {"qwen38-flash-next": _dep("openai/qwen38-flash-next")})
    data = {"model": "qwen38-flash-next",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
    hook._apply_family_sampling(data, "qwen38-flash-next")
    assert data["temperature"] == 0.6 and data["top_p"] == 0.95


def test_estructurada_nunca_usa_el_perfil_de_pensar(hook):
    """El esquema apaga el pensamiento pase lo que pase (medido 3/3 el 10-08),
    asi que pedir `max` y un json_schema no puede dar el perfil de pensar. Y con
    esquema no se pisa el sampling del cliente en absoluto."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = _structured(temperature=0, reasoning_effort="max")
    hook._apply_family_sampling(data, "tooling")
    assert data["temperature"] == 0
    assert hook._thinking_is_on(data, "tooling") is False
def test_sin_saber_si_piensa_manda_el_perfil_de_siempre(hook):
    """Un alias fuera de THINKING_TIERS y sin effort deja el pensamiento al
    default del SERVIDOR. Desde aqui no se sabe, y adivinar el perfil seria
    peor que quedarse con el de hoy.

    05-09-2026 (SC-203): la sonda pasa a `max`. Con `tooling` ya no se puede
    sondear el "no lo se": su default es `low` desde hoy, asi que
    `_thinking_is_on` responde True (lo sabe) y el perfil es el de pensar."""
    _install_fake_litellm({"max": _dep("openai/qwen38-flash-next")})
    assert hook._thinking_is_on({"model": "max"}, "dense") is None
    data = {"model": "max"}
    hook._apply_family_sampling(data, "max")
    assert data["temperature"] == 0.7


def test_los_dos_perfiles_de_una_familia_traen_las_mismas_claves(hook):
    """Si `set_thinking` olvidara una clave que `set` si pone, esa clave se
    quedaria sin fijar justo en el modo que la necesita igual."""
    for fam, prof in hook.FAMILY_SAMPLING.items():
        thinking = prof.get("set_thinking")
        if thinking is not None:
            assert set(thinking) == set(prof["set"]), fam


# ── Bucle de token 0 con tools (sglang#36537, 2026-08-28) ─────────────────────
#
# Con thinking + tools + `--tool-call-parser qwen3_coder` --las tres a la vez--
# Qwen3.8-Flash-Next entra en un bucle DETERMINISTA de token id 0, que con este
# tokenizer decodifica como `!`. La respuesta sale como `...tool!!!!!!!!` hasta
# max_tokens, con tool_calls null y finish_reason length. El servidor corre con
# TOOL_CALL_PARSER=qwen3_coder, asi que la tercera condicion esta siempre puesta
# y la unica que podemos quitar desde aqui es el pensamiento.


def _tools(**extra):
    d = {"model": "tooling",
         "tools": [{"type": "function",
                    "function": {"name": "shell_probe", "parameters": {}}}]}
    d.update(extra)
    return d


def test_con_tools_qwen_si_piensa_el_effort_pedido(hook):
    """El caso de opencode: manda tools en cada turno y un effort alto.

    05-09-2026 (SC-203): ANTES este test fijaba lo contrario — tools forzaban
    `off` por sglang#36537 (bucle determinista de token id 0 con thinking +
    tools + qwen3_coder). La reproduccion limpia de QA de hoy, con y sin el
    guard activo, dio `tool_calls` bien formados en ambos casos y ningun bucle:
    el guard se retira, y lo que fija el test es la traduccion normal del
    effort (`high` -> `low` del backend) CON tools presentes. Si el bucle del
    issue volviera con una subida de SGLang, esto es un test rojo."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = _tools(reasoning_effort="high")
    hook._apply_thinking_tier(data, "tooling")
    assert _ctk(data) == hook.THINKING_KWARGS["qwen"]["high"]
def test_sin_tools_qwen_sigue_pensando_si_se_lo_piden(hook):
    """La puerta es SOLO para tools: sin ellas el effort manda como siempre.

    El `high` del cliente viaja como `low` al backend, que es el unico nivel
    acotado que este template sabe instruir. Ver la nota en
    test_qwen_no_gradua_pero_si_enciende_y_apaga."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = {"model": "tooling", "reasoning_effort": "high"}
    hook._apply_thinking_tier(data, "tooling")
    assert _ctk(data) == {"enable_thinking": True, "reasoning_effort": "low"}


def test_con_tools_el_sampling_usa_el_perfil_de_pensar(hook):
    """Las dos mitades tienen que decir lo mismo (05-09-2026, SC-203).

    ANTES: tools implicaba no-pensar (guard sglang#36537) -> perfil 0.7/0.8.
    Con el guard retirado, tools + effort alto PIENSA -> el perfil es el de
    pensar, 0.6/0.95. La propiedad fijada no cambia: `_thinking_is_on` y
    `_apply_thinking_tier` tienen que responder lo MISMO sobre las mismas
    entradas; lo que cambia es la respuesta."""
    _install_fake_litellm({"tooling": _dep("openai/qwen38-flash-next")})
    data = _tools(reasoning_effort="high")
    hook._apply_family_sampling(data, "tooling")
    assert data["temperature"] == 0.6 and data["top_p"] == 0.95
    assert hook._thinking_is_on(data, "tooling") is True


def test_detector_de_tools(hook):
    assert hook._has_tools({"tools": [{"type": "function"}]})
    assert not hook._has_tools({"tools": []})
    assert not hook._has_tools({})
