"""Contrato de la OMISION de imagen para backends sin vision (SC-173, 2026-09-05).

Que fija este contrato, y por que existe
----------------------------------------
`_vision_target` intenta DESVIAR una peticion con imagen a un backend que ve.
Cuando no hay a donde desviar (VISION_FALLBACK_MODEL vacio, o apuntando al MISMO
backend ciego -- el estado del cluster hoy), la peticion llega al backend que
DECLARA `supports_vision: False` y el turno muere con un 400 del motor PARA
SIEMPRE en esa sesion: la imagen queda pegada en el historial y cada turno
siguiente vuelve a fallar.

`_omit_images_for_blind_backends` es la red final: si el backend resuelto declara
que NO ve, sustituye cada bloque de imagen por un texto y el turno sigue. Es la
opcion 2 de la epica SC-170, tras descartar la 1 (un ajuste estandar de LiteLLM
que descarte la imagen) con motivo: en v1.96.0 `supports_vision: false` NO hace
que el router descarte la imagen en la ruta Anthropic->chat-completions; solo
afecta a la seleccion de prompt-template por proveedor (gemini) y a la publicacion
de capacidades. No existe un `drop_params` que quite bloques `image`.

Lo que se puede romper sin darse cuenta, y este test no deja:

  - con el backend ciego (False explicito) hay que omitir;
  - con el backend que VE (True) no se toca nada -- no se le quita al multimodal
    una imagen que si procesa (el residente qwen38-flash-next ve hoy);
  - con "no se sabe" (None) NO se toca nada, igual que `_vision_target` respeta un
    nombre de MODELO sin veredicto;
  - se barren las TRES formas de parte de imagen (image / image_url / input_image)
    y en las dos formas de payload (messages y input);
  - el texto de relleno es el que promete la epica, no un placeholder vacio.

Se carga el hook REAL desde el manifest y se ejecutan solo sus funciones puras,
inyectando la sonda que toca internals de litellm (mismo truco que
tests/test_vision_routing_contract.py).
"""
import ast
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {
    "_omit_images_for_blind_backends",
    "_message_entries",
    # sonda por defecto: tiene que existir para que la firma del `def` ligue.
    "_alias_supports_vision",
}
WANT_CONST = {"IMAGE_PART_TYPES", "IMAGE_OMITTED_TEXT"}


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
    mod = types.ModuleType("hookomit")
    mod.__dict__["os"] = __import__("os")
    mod.__dict__["log"] = __import__("logging").getLogger("test")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


def _con_imagen_anthropic():
    # Forma Anthropic /v1/messages: parte `image` con `source`.
    return {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "describe esto"},
        {"type": "image", "source": {"type": "base64",
                                      "media_type": "image/png", "data": "AAAA"}}]}]}


def _con_imagen_chat():
    # Forma Chat Completions: parte `image_url`.
    return {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "que ves?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}


def _con_imagen_responses():
    # Forma Responses API: `input` con parte `input_image`.
    return {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "que ves?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]}


def _omit(hook, data, served, ve):
    return hook._omit_images_for_blind_backends(
        data, served, supports_vision=lambda alias: ve)


def test_backend_ciego_omite_la_imagen_anthropic(hook):
    data = _con_imagen_anthropic()
    assert _omit(hook, data, "tooling", ve=False) is True
    content = data["messages"][0]["content"]
    # ninguna parte de imagen sobrevive
    assert not [p for p in content if p.get("type") in hook.IMAGE_PART_TYPES]
    # y el texto de relleno es el que promete la epica, no un placeholder vacio
    texts = [p["text"] for p in content if p.get("type") == "text"]
    assert hook.IMAGE_OMITTED_TEXT in texts
    assert hook.IMAGE_OMITTED_TEXT == "[imagen omitida: el modelo local no procesa imagenes]"


def test_backend_ciego_omite_la_imagen_chat(hook):
    data = _con_imagen_chat()
    assert _omit(hook, data, "tooling", ve=False) is True
    content = data["messages"][0]["content"]
    assert not [p for p in content if p.get("type") in hook.IMAGE_PART_TYPES]


def test_backend_ciego_omite_la_imagen_responses(hook):
    data = _con_imagen_responses()
    assert _omit(hook, data, "tooling", ve=False) is True
    content = data["input"][0]["content"]
    assert not [p for p in content if p.get("type") in hook.IMAGE_PART_TYPES]


def test_backend_que_ve_no_se_toca_nada(hook):
    """El residente qwen38-flash-next VE hoy: quitarle la imagen seria un bug.

    Este es el caso que separa el arreglo de una regresion: con supports_vision
    True el turno tiene que salir con la imagen intacta, no omitida.
    """
    for maker in (_con_imagen_anthropic, _con_imagen_chat, _con_imagen_responses):
        data = maker()
        assert _omit(hook, data, "qwen38-flash-next", ve=True) is False, maker
        # nada de texto de relleno
        flat = str(data)
        assert hook.IMAGE_OMITTED_TEXT not in flat, maker


def test_sin_veredicto_no_se_toca_nada(hook):
    """None ('no se sabe') = no se omite. Mismo criterio que `_vision_target`.

    No se le quita al cliente una imagen que el backend podria si procesar; solo
    se actua con el False EXPLICITO.
    """
    data = _con_imagen_anthropic()
    assert _omit(hook, data, "ornith-1.0", ve=None) is False
    assert hook.IMAGE_OMITTED_TEXT not in str(data)


def test_sin_imagen_no_toca_nada(hook):
    data = {"messages": [{"role": "user", "content": "solo texto"}]}
    assert _omit(hook, data, "tooling", ve=False) is False
    assert data["messages"][0]["content"] == "solo texto"


def test_conserva_las_partes_de_texto_y_el_orden(hook):
    """La imagen se sustituye, no se borra: el texto alrededor se conserva."""
    data = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "antes"},
        {"type": "image", "source": {"type": "base64",
                                      "media_type": "image/png", "data": "AAAA"}},
        {"type": "text", "text": "despues"}]}]}
    assert _omit(hook, data, "tooling", ve=False) is True
    content = data["messages"][0]["content"]
    assert [p["type"] for p in content] == ["text", "text", "text"]
    assert content[0]["text"] == "antes"
    assert content[1]["text"] == hook.IMAGE_OMITTED_TEXT
    assert content[2]["text"] == "despues"


def test_la_funcion_esta_cableada_en_el_pre_call_hook(hook):
    """La red final se llama de verdad, no es codigo muerto en el manifest.

    Sin esto, definir la funcion y no invocarla pasa todos los tests de arriba y
    deja el bug intacto en produccion. Se comprueba en el fuente del hook que
    `_omit_images_for_blind_backends` se invoca dentro de `async_pre_call_hook`.
    """
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(d["data"]["litellm_strip_params.py"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    # localizar async_pre_call_hook dentro de la clase StripUnsupportedParams
    hook_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_pre_call_hook":
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    hook_calls.add(call.func.id)
    assert "_omit_images_for_blind_backends" in hook_calls, (
        "_omit_images_for_blind_backends no se invoca en async_pre_call_hook: "
        "la red final esta definida pero cableada a nada")
