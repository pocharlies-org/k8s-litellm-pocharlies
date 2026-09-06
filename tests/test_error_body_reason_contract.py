"""Contrato del MOTIVO del error de upstream (SC-174, 2026-09-05).

Que fija este contrato, y por que existe
----------------------------------------
Cuando vLLM rechaza un payload, LiteLLM traduce la excepcion de forma OpenAI a
forma Anthropic y, en ciertos casos, le llega al cliente un cuerpo vacio:
`OpenAIException - .` sin motivo. Es un defecto en si mismo: incluso cuando el
turno no muere (ver la historia hermana de imagenes), CUALQUIER 400 de upstream
debe devolver el motivo, no un mensaje vacio.

Donde se pierde el cuerpo (v1.96.0, la imagen desplegada):
`get_error_message` (litellm/litellm_core_utils/exception_mapping_utils.py:124-167)
solo lee `body["message"]` y `body["error"]["message"]`. Un 400 de validacion de
vLLM/FastAPI viene como `{"detail": [...]}` (o `{"detail": "..."}`), que esa
funcion NO reconoce -> devuelve None -> el fallback `message = str(exc)` sale
vacio o repr -> `OpenAIException - .`. El cuerpo SI sobrevive en la excepcion
(`BadRequestError(body=...)`, linea 342 del mismo fichero), o sea que se puede
recuperar en el failure hook.

Lo que se puede romper sin darse cuenta, y este test no deja:

  - un cuerpo FastAPI `{"detail": [...]}` (pydantic) tiene que dar un motivo que
    nombre el campo rechazado y la palabra 'validation'/'valid';
  - el mensaje degenerado `OpenAIException - .` (con o sin el sufijo
    '. Received Model Group=' que anade el router) se detecta como tal;
  - un mensaje que YA trae motivo NO se pisa (los 400 cuyo cuerpo venia en forma
    OpenAI salen bien y quedan intactos);
  - `_extract_upstream_reason` nunca lanza: con basura devuelve None.

Se cargan las funciones PURAS desde el hook real del manifest (mismo truco AST
que tests/test_vision_routing_contract.py). La reconstruccion se cablea MUTANDO
`original_exception.message` en `async_post_call_failure_hook`, porque el
`except Exception` de /v1/messages descarta el retorno del hook y relee
`e.message` sobre el MISMO objeto; eso lo fija el ultimo test.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_extract_upstream_reason", "_is_degenerate_error_message"}
WANT_CONST = {"_ROUTER_ERROR_SUFFIX"}


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
    mod = types.ModuleType("hookerr")
    mod.__dict__["os"] = __import__("os")
    mod.__dict__["log"] = __import__("logging").getLogger("test")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


class FakeExc(Exception):
    """Imita la forma de una litellm.BadRequestError: .message y .body."""

    def __init__(self, message="", body=None):
        super().__init__(message)
        self.message = message
        self.body = body


# El mensaje EXACTO que veia el cliente en la epica (reproducido 2026-09-04):
DEGENERATE = ("litellm.BadRequestError: OpenAIException - . "
              "Received Model Group=qwen38-flash-next\n"
              "Available Model Group Fallbacks=None")


def test_detecta_el_mensaje_degenerado(hook):
    assert hook._is_degenerate_error_message(DEGENERATE) is True
    assert hook._is_degenerate_error_message("OpenAIException - .") is True
    assert hook._is_degenerate_error_message("OpenAIException - ") is True
    assert hook._is_degenerate_error_message("") is True
    assert hook._is_degenerate_error_message(None) is True


def test_respeta_un_mensaje_que_ya_trae_motivo(hook):
    """No se pisa un 400 que litellm ya traduzco bien (cuerpo en forma OpenAI).

    Reproducido en vivo el 2026-09-05: `max_tokens=99999999 cannot be greater
    than max_model_len=...` ya sale con motivo y NO debe tocarse.
    """
    good = ("litellm.BadRequestError: OpenAIException - max_tokens=99999999 "
            "cannot be greater than max_model_len=max_total_tokens=262144. "
            "Received Model Group=qwen38-flash-next")
    assert hook._is_degenerate_error_message(good) is False


def test_extrae_motivo_de_cuerpo_pydantic_detail_lista(hook):
    """El caso de la epica: vLLM rechaza con {'detail': [ ... ]} y litellm lo deja vacio."""
    exc = FakeExc(
        message=DEGENERATE,
        body={"detail": [
            {"loc": ["body", "messages", 0, "content", 1, "type"],
             "msg": "Input should be 'image_url'", "type": "literal_error"},
            {"loc": ["body", "messages", 0, "content", 1, "image_url"],
             "msg": "Field required", "type": "missing"},
        ]},
    )
    reason = hook._extract_upstream_reason(exc)
    assert reason, "no se extrajo motivo de un cuerpo pydantic"
    # nombra el campo rechazado y el tipo de error, no es generico
    assert "messages" in reason
    assert "Input should be" in reason or "Field required" in reason


def test_extrae_motivo_de_cuerpo_detail_string(hook):
    exc = FakeExc(message=DEGENERATE,
                  body={"detail": "1 validation error: 'type' 'input_image' Input should be a valid string"})
    reason = hook._extract_upstream_reason(exc)
    assert reason and "input_image" in reason and "valid string" in reason


def test_extrae_motivo_de_cuerpo_openai(hook):
    exc = FakeExc(message=DEGENERATE,
                  body={"error": {"message": "Invalid temperature value"}})
    reason = hook._extract_upstream_reason(exc)
    assert reason and "temperature" in reason


def test_cae_a_str_de_la_excepcion_sin_cuerpo(hook):
    """Sin body utilizable, usa el str() de la excepcion si dice algo real."""
    exc = FakeExc(message="")
    exc.args = ("engine is overloaded, try again later",)
    reason = hook._extract_upstream_reason(exc)
    assert reason and "overloaded" in reason


def test_devuelve_none_con_basura(hook):
    """Nunca lanza: con excepcion sin cuerpo ni mensaje, None (flujo queda igual)."""
    assert hook._extract_upstream_reason(FakeExc(message="")) is None
    assert hook._extract_upstream_reason(None) is None


def test_el_hook_reconstruye_mutando_la_excepcion(hook):
    """El cableado real: el failure hook MUTA original_exception.message.

    No se puede ejecutar async_post_call_failure_hook entero sin el runtime de
    litellm, asi que se comprueba en el AST del hook que DENTRO de ese metodo:
      - se llama a _is_degenerate_error_message y a _extract_upstream_reason, y
      - se ASIGNA `original_exception.message` (mutacion, no retorno), porque el
        `except Exception` de /v1/messages descarta el retorno del hook y relee
        `e.message` sobre el MISMO objeto.
    """
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(d["data"]["litellm_strip_params.py"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    body = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_post_call_failure_hook":
            body = node
            break
    assert body is not None, "no existe async_post_call_failure_hook"
    called = {c.func.id for c in ast.walk(body)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "_is_degenerate_error_message" in called
    assert "_extract_upstream_reason" in called
    # tiene que haber una asignacion a original_exception.message
    mutates = any(
        isinstance(t, ast.Attribute) and t.attr == "message"
        and isinstance(t.value, ast.Name) and t.value.id == "original_exception"
        for node in ast.walk(body) if isinstance(node, ast.Assign)
        for t in node.targets
    )
    assert mutates, (
        "el failure hook no muta original_exception.message: sin la mutacion el "
        "cliente sigue viendo el mensaje vacio, porque /v1/messages descarta el "
        "retorno del hook")
