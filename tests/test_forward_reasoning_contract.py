"""El thinking historico tiene que salir hacia vLLM en el campo `reasoning`.

vLLM lee el razonamiento previo de `message["reasoning"]` y NO de
`reasoning_content` (en su ConversationMessage ese nombre esta marcado
"Deprecated": solo se ESCRIBE en la salida). LiteLLM emite `reasoning_content`
tanto por el adaptador openai/ como por la traduccion de /v1/messages, asi que
sin este renombrado el razonamiento llega al backend y se descarta en silencio,
antes de la plantilla y sin error. Medido con /tokenize sobre una secuencia de
herramientas: 321 tokens sin thinking == 321 con `reasoning_content` -> 472 con
`reasoning`.

Este contrato existe porque el fallo es MUDO: si alguien renombra el campo o
retira el kwarg, nada peta y el unico sintoma es que el modelo vuelve a razonar
lo ya razonado.
"""

import ast
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
MARKER = "def _forward_reasoning_to_vllm("


def _configmap_value(marker: str) -> str:
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for content in (doc.get("data") or {}).values():
            if marker in content:
                return content
    raise AssertionError(f"no encuentro un ConfigMap con {marker!r}")


def _hook_source() -> str:
    return _configmap_value(MARKER)


def _load_forwarder():
    """Ejecuta la funcion REAL del manifiesto, no una copia escrita aqui."""
    tree = ast.parse(_hook_source())
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_forward_reasoning_to_vllm"),
        None,
    )
    assert fn is not None, "no encuentro _forward_reasoning_to_vllm en el manifiesto"

    class _Log:
        def info(self, *a, **k):
            pass

    scope: dict = {"router_log": _Log()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<hook>", "exec"), scope)
    return scope["_forward_reasoning_to_vllm"]


def _forward_reasoning_models() -> set:
    tree = ast.parse(_hook_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(t, "id", "") == "FORWARD_REASONING_MODELS" for t in node.targets):
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError("no encuentro FORWARD_REASONING_MODELS")


def _strict_leading_system_models() -> set:
    tree = ast.parse(_hook_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(getattr(t, "id", "") == "STRICT_LEADING_SYSTEM_MODELS" for t in node.targets):
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError("no encuentro STRICT_LEADING_SYSTEM_MODELS")


def _assistant(reasoning_content=None, thinking_blocks=None):
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "f", "arguments": "{}"}}
        ],
    }
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    if thinking_blocks is not None:
        msg["thinking_blocks"] = thinking_blocks
    return msg


def _data(*messages):
    return {"model": "qwen38-flash-next", "messages": list(messages)}


def test_renombra_reasoning_content_al_campo_que_vllm_lee():
    forward = _load_forwarder()
    data = _data(
        {"role": "user", "content": "haz algo"},
        _assistant(reasoning_content="PENSADO ANTES"),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    )
    assert forward(data) is True
    assert data["messages"][1]["reasoning"] == "PENSADO ANTES"


def test_acota_el_thinking_al_turno_en_curso():
    """`preserve_thinking: False` = solo el thinking posterior al ultimo `user`.

    Es lo que recomienda Qwen y lo que impide que el contexto de una sesion
    agentica larga crezca sin tope acumulando el razonamiento de cada turno.
    """
    forward = _load_forwarder()
    data = _data(
        {"role": "user", "content": "haz algo"},
        _assistant(reasoning_content="PENSADO ANTES"),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    )
    forward(data)
    assert data["extra_body"]["chat_template_kwargs"]["preserve_thinking"] is False


def test_no_pisa_el_preserve_thinking_del_cliente():
    forward = _load_forwarder()
    data = _data(
        {"role": "user", "content": "haz algo"},
        _assistant(reasoning_content="PENSADO ANTES"),
    )
    data["extra_body"] = {"chat_template_kwargs": {"preserve_thinking": True}}
    forward(data)
    assert data["extra_body"]["chat_template_kwargs"]["preserve_thinking"] is True


def test_retira_los_thinking_blocks_duplicados():
    """La traduccion de /v1/messages manda el MISMO texto dos veces.

    vLLM no lee `thinking_blocks`: dejarlos solo es peso en el JSON.
    """
    forward = _load_forwarder()
    data = _data(
        {"role": "user", "content": "haz algo"},
        _assistant(
            reasoning_content="PENSADO ANTES",
            thinking_blocks=[{"type": "thinking", "thinking": "PENSADO ANTES",
                              "signature": "s"}],
        ),
    )
    forward(data)
    assert "thinking_blocks" not in data["messages"][1]


def test_es_noop_sin_thinking_en_el_historial():
    """Sin razonamiento previo no se toca extra_body: ni kwarg, ni cambio de prompt."""
    forward = _load_forwarder()
    data = _data(
        {"role": "user", "content": "haz algo"},
        _assistant(),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    )
    assert forward(data) is False
    assert "extra_body" not in data


def test_ignora_el_reasoning_content_vacio():
    forward = _load_forwarder()
    data = _data(
        {"role": "user", "content": "haz algo"},
        _assistant(reasoning_content="   "),
    )
    assert forward(data) is False
    assert "reasoning" not in data["messages"][1]
    assert "reasoning_content" not in data["messages"][1]


def test_no_toca_los_mensajes_de_otros_roles():
    forward = _load_forwarder()
    data = _data(
        {"role": "user", "content": "haz algo", "reasoning_content": "no es mio"},
        _assistant(reasoning_content="PENSADO ANTES"),
    )
    forward(data)
    assert "reasoning" not in data["messages"][0]
    assert data["messages"][0]["reasoning_content"] == "no es mio"


def test_los_alias_cubiertos_son_los_del_backend_qwen38_flash_next():
    """Mismo backend y mismo chat template => mismo trato.

    `STRICT_LEADING_SYSTEM_MODELS` ya describe ese par por el mismo motivo. Si
    entra un alias nuevo sobre ese vLLM y solo se apunta en una de las dos
    listas, el thinking se pierde por ese alias y en silencio.
    """
    assert _forward_reasoning_models() == _strict_leading_system_models()


def test_los_alias_existen_en_el_model_list():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    names = set()
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for content in (doc.get("data") or {}).values():
            if "model_list:" not in content:
                continue
            parsed = yaml.safe_load(content)
            for entry in (parsed or {}).get("model_list", []) or []:
                names.add(entry.get("model_name"))
    assert _forward_reasoning_models() <= names


def test_el_hook_llama_al_forwarder_con_el_modelo_resuelto():
    """Llamarlo con el alias PEDIDO seria un no-op para tooling/high/max."""
    src = _hook_source()
    assert 'if data.get("model") in FORWARD_REASONING_MODELS:' in src
    assert "_forward_reasoning_to_vllm(data)" in src
    # Antes del sello: escribe en extra_body y _preserve_uncensored_seal lo repone.
    # rindex: la ULTIMA ocurrencia es la llamada del hook, no la definicion.
    assert src.rindex("_forward_reasoning_to_vllm(data)") < src.rindex(
        "_preserve_uncensored_seal(data)"
    )
