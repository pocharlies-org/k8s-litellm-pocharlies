"""Contrato del scrubbing de PDFs colados como imagen (15-09-2026).

Que fija este contrato, y por que existe
----------------------------------------
Claude Code lee un PDF y lo manda como bloque `document` (Anthropic). El
adaptador Anthropic->OpenAI de litellm (transformation.py:427 en la v1.100.0
desplegada) lo convierte en un `image_url` con los bytes del PDF TAL CUAL: no
lo renderiza. El motor lo pasa a PIL y contesta

    400 Failed to load image: cannot identify image file <_io.BytesIO object>

Medido en vivo el 15-09: una sesion haciendo `Read bank_info.pdf` contra
`qwen38-flash-next` murio en cada reintento — el PDF viaja pegado en el
historial y el grupo no tiene fallbacks. Ni el desvio de vision ni
`_omit_images_for_blind_backends` (SC-173) lo interceptan: el alias declara
`supports_vision: true`, y para el router ese backend VE; lo que no puede es
DECODIFICAR un PDF.

`_scrub_pdf_documents` es la red para ese caso: sustituye cada PDF colado como
imagen por un texto que le dice al modelo como extraerse el contenido solo
(pdftotext / pdftoppm). Convierte un 400 terminal en un turno recuperable.

Lo que se puede romper sin darse cuenta, y este test no deja:

  - las TRES formas de parte (Anthropic `image`/`document` con source, Chat
    Completions `image_url` con data URI, Responses `input_image`) tienen que
    ser detectadas: el hook ve el payload antes o despues de la conversion
    segun la ruta;
  - el PDF anidado dentro de `tool_result` (donde Claude Code engancha el PDF
    del Read) tambien tiene que ser barrido;
  - una imagen PNG/JPEG de verdad NO se toca: el residente ve imagenes y
    cargar una foto es el caso normal;
  - el payload sin PDFs queda intacto y la funcion devuelve False (cero
    riesgo para el 99,9 % de peticiones que no traen documentos);
  - el cableado: `async_pre_call_hook` llama a `_scrub_pdf_documents` ANTES
    del desvio de vision — si se mueve despues, un PDF solo vuelve a provocar
    desvios de vision a un destino que tampoco lo decodifica.

Se cargan las funciones PURAS desde el hook real del manifest (mismo truco AST
que tests/test_vision_image_omission_contract.py).
"""
import ast
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_scrub_pdf_documents", "_is_pdf_part", "_message_entries"}
WANT_CONST = {"PDF_OMITTED_TEXT"}


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
    mod = types.ModuleType("hookpdf")
    mod.__dict__["os"] = __import__("os")
    mod.__dict__["log"] = __import__("logging").getLogger("test")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


PDF_B64 = "JVBERi0xLjQK"  # encabezado "%PDF-1.4" en base64


def _pdf_forma_document():
    """Forma Anthropic cruda: bloque `document` (la que manda Claude Code)."""
    return {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "mira el adjunto"},
        {"type": "document", "source": {"type": "base64",
                                        "media_type": "application/pdf",
                                        "data": PDF_B64}}]}]}


def _pdf_forma_image_url():
    """Forma Chat Completions: lo que deja el adaptador (image_url con PDF)."""
    return {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "mira el adjunto"},
        {"type": "image_url", "image_url": {
            "url": f"data:application/pdf;base64,{PDF_B64}"}}]}]}


def _pdf_forma_input_image():
    """Forma Responses API."""
    return {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "mira"},
        {"type": "input_image",
         "image_url": f"data:application/pdf;base64,{PDF_B64}"}]}]}


def _pdf_dentro_de_tool_result():
    """El caso real del Read: el PDF anidado en el tool_result."""
    return {"messages": [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf",
                    "data": PDF_B64}}]}]},
    ]}


def _con_png_de_verdad():
    return {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "que ves?"},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,iVBORw0KGgo"}}]}]}


def test_omite_pdf_forma_document(hook):
    data = _pdf_forma_document()
    assert hook._scrub_pdf_documents(data) is True
    content = data["messages"][0]["content"]
    assert not [p for p in content if p.get("type") == "document"]
    texts = [p["text"] for p in content if p.get("type") == "text"]
    assert hook.PDF_OMITTED_TEXT in texts


def test_omite_pdf_forma_image_url(hook):
    data = _pdf_forma_image_url()
    assert hook._scrub_pdf_documents(data) is True
    content = data["messages"][0]["content"]
    assert not [p for p in content if p.get("type") == "image_url"]


def test_omite_pdf_forma_input_image(hook):
    data = _pdf_forma_input_image()
    assert hook._scrub_pdf_documents(data) is True
    content = data["input"][0]["content"]
    assert not [p for p in content if p.get("type") == "input_image"]


def test_omite_pdf_anidado_en_tool_result(hook):
    """El Read de Claude Code engancha el PDF DENTRO del tool_result."""
    data = _pdf_dentro_de_tool_result()
    assert hook._scrub_pdf_documents(data) is True
    tr = data["messages"][0]["content"][0]
    texts = [p["text"] for p in tr["content"] if p.get("type") == "text"]
    assert hook.PDF_OMITTED_TEXT in texts


def test_no_toca_un_png_de_verdad(hook):
    """El residente ve imagenes: una foto real no se le quita jamas."""
    data = _con_png_de_verdad()
    before = ast.dump(ast.parse(repr(data)))  # snapshot estructural
    assert hook._scrub_pdf_documents(data) is False
    assert ast.dump(ast.parse(repr(data))) == before


def test_payload_sin_pdfs_devuelve_false(hook):
    data = {"messages": [{"role": "user", "content": "hola"}]}
    assert hook._scrub_pdf_documents(data) is False


def test_el_texto_instruye_como_extraer(hook):
    """El reemplazo no es un placeholder vacio: dice pdftotext/pdftoppm."""
    assert "pdftotext" in hook.PDF_OMITTED_TEXT
    assert "pdftoppm" in hook.PDF_OMITTED_TEXT


def test_el_hook_llama_antes_del_desvio_de_vision(hook):
    """Cableado: `_scrub_pdf_documents` va ANTES de `_vision_target`.

    Si se mueve despues, un PDF solo vuelve a provocar desvios de vision a un
    destino que tampoco lo decodifica, y el flag `_pdf_documents_scrubbed`
    llegaría tarde al metadata de la peticion ya desviada.
    """
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    src = next(d["data"]["litellm_strip_params.py"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    tree = ast.parse(src)
    body = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_pre_call_hook"),
                None)
    assert body is not None, "no existe async_pre_call_hook"
    calls = [(n.lineno, n.func.id) for n in ast.walk(body)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ("_scrub_pdf_documents", "_vision_target")]
    scrubs = [ln for ln, name in calls if name == "_scrub_pdf_documents"]
    diverts = [ln for ln, name in calls if name == "_vision_target"]
    assert scrubs, "async_pre_call_hook ya no llama a _scrub_pdf_documents"
    assert diverts, "async_pre_call_hook ya no llama a _vision_target"
    assert max(scrubs) < min(diverts), (
        "_scrub_pdf_documents se llama despues del desvio de vision: un PDF "
        "solo volveria a desviarse a un backend que tampoco lo decodifica")
