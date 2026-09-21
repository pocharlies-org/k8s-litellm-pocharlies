"""Una imagen gorda NO puede irse a Alibaba: se rechaza en el hook.

OWU-50 (21-09-2026). Dani veia `API Error: 400 litellm.BadRequestError:
OpenAIException - "Download multimodal file timed out"` en varias sesiones,
**solo** con proveedores de Alibaba. Reproducido contra `alibaba-q38-flash` con
la misma peticion y solo cambiando el tamano del PNG:

    16 KB -> HTTP 200     3,0 MB -> HTTP 200     12,3 MB -> 400 (el timeout)

DashScope va a buscar el fichero multimodal a su lado y tiene un tope de tiempo
para descargarlo; el residente local decodifica el base64 en su propio proceso y
no descarga nada. De ahi que el fallo sea exclusivo de la ruta de nube.

Y por que la sesion se queda reintentando en vez de degradar: el mapa
`router_settings.fallbacks` solo declara `qwen38-flash-next -> [alibaba-q38-flash]`,
asi que un grupo `alibaba-*` **no tiene a donde caer**, y un 400 no es reintenable
por politica de reintentos: el cliente vuelve a mandar el mismo payload gordo.

Este contrato vigila el corte. Lo que NO hace es reescalar: la imagen oficial de
LiteLLM no trae Pillow (comprobado en el contenedor), y sin decodificador no hay
forma de reducir un JPEG/PNG en un hook. Reescalar de verdad exige imagen propia,
y eso se decide aparte.
"""
import ast
import pathlib
import types

import pytest
import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# Lo que se ejecuta del hook: el helper y su constante, sin importar litellm.
WANT_NAMES = {
    "ALIBABA_MULTIMODAL_MAX_BYTES",
    "_b64_payload_len",
    "_inline_image_sizes",
    "_alibaba_multimodal_too_large",
}


@pytest.fixture(scope="module")
def hook_src():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if (
            doc
            and doc.get("kind") == "ConfigMap"
            and doc["metadata"]["name"] == "litellm-config"
        ):
            return doc["data"]["litellm_strip_params.py"]
    raise AssertionError("no encuentro el ConfigMap litellm-config")


@pytest.fixture(scope="module")
def hook(hook_src):
    keep = []
    for node in ast.parse(hook_src).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") in WANT_NAMES for t in node.targets
        ):
            keep.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANT_NAMES:
            keep.append(node)
    presentes = set()
    for n in keep:
        if isinstance(n, ast.Assign):
            presentes |= {getattr(t, "id", "") for t in n.targets}
        else:
            presentes.add(n.name)
    faltan = WANT_NAMES - presentes
    assert not faltan, f"el hook ya no define: {sorted(faltan)}"
    mod = types.ModuleType("hook_alibaba_multimodal")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


def _payload(model, nbytes, shape="openai"):
    b64 = "A" * int(nbytes * 4 / 3)
    if shape == "openai":
        part = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
    else:  # forma Anthropic
        part = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
    return {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "que hay en la imagen"}, part]}]}


# ── el umbral, anclado a lo medido ───────────────────────────────────────────

def test_el_limite_esta_dentro_de_la_banda_medida(hook):
    """3 MB pasa y 12 MB no: el corte tiene que estar entre las dos cosas, y mas
    cerca de la que funciona. Un limite por encima de 12 MB no cortaria nada."""
    limite = hook.ALIBABA_MULTIMODAL_MAX_BYTES
    assert 2 * 1024 * 1024 <= limite <= 6 * 1024 * 1024, (
        f"ALIBABA_MULTIMODAL_MAX_BYTES={limite} fuera de la banda medida "
        f"(3 MB sirve, 12 MB da timeout)"
    )


def test_lo_que_medido_como_bueno_pasa(hook):
    ok, _ = hook._alibaba_multimodal_too_large("alibaba-q38-flash", _payload("alibaba-q38-flash", 3 * 1024 * 1024))
    assert not ok, "una imagen de 3 MB (HTTP 200 medido) no puede rechazarse"


def test_lo_que_medido_como_roto_se_corta(hook):
    ok, detail = hook._alibaba_multimodal_too_large(
        "alibaba-q38-flash", _payload("alibaba-q38-flash", 12 * 1024 * 1024))
    assert ok and detail["error"] == "image_too_large_for_alibaba"
    assert detail["image_mb"] >= 12 and detail["limit_mb"] >= 2
    assert "residente local" in detail["hint"], "el mensaje tiene que decir que hacer"


def test_una_imagen_pequena_no_se_toca(hook):
    ok, _ = hook._alibaba_multimodal_too_large("alibaba-q38-flash", _payload("alibaba-q38-flash", 16 * 1024))
    assert not ok


# ── el corte es SOLO para la ruta de nube ────────────────────────────────────

@pytest.mark.parametrize("alias", ["qwen38-flash-next", "tooling", "q38-flash-u", "q38-flash-u-think"])
def test_al_residente_local_le_puede_llegar_grande(hook, alias):
    """El vLLM decodifica el base64 en proceso: cortarle a él seria una regresion
    disfrazada de arreglo. El criterio es el prefijo del destino, no el tamano."""
    ok, _ = hook._alibaba_multimodal_too_large(alias, _payload(alias, 12 * 1024 * 1024))
    assert not ok, f"{alias} es local y no debe rechazararse por tamano"


@pytest.mark.parametrize("alias", ["alibaba-q38-flash", "alibaba-q38-max", "alibaba-q37-plus"])
def test_toda_la_ruta_alibaba_esta_cubierta(hook, alias):
    ok, _ = hook._alibaba_multimodal_too_large(alias, _payload(alias, 12 * 1024 * 1024))
    assert ok, f"{alias} tambien descarga la imagen a su lado"


# ── formas del payload ───────────────────────────────────────────────────────

def test_la_forma_anthropic_tambien_se_mide(hook):
    """Por si el hook ve la peticion antes de traducir: `source.data` en base64."""
    ok, _ = hook._alibaba_multimodal_too_large(
        "alibaba-q38-flash", _payload("alibaba-q38-flash", 12 * 1024 * 1024, shape="anthropic"))
    assert ok


def test_sin_imagen_no_hay_nada_que_cortar(hook):
    data = {"model": "alibaba-q38-flash", "messages": [{"role": "user", "content": "hola"}]}
    assert hook._alibaba_multimodal_too_large("alibaba-q38-flash", data) == (False, None)


def test_una_url_remota_no_se_corta_aqui(hook):
    """Su tamano lo vigila LiteLLM con MAX_IMAGE_URL_DOWNLOAD_SIZE_MB; medirlo dos
    veces seria meter una descarga sincrona en el pre-call hook."""
    data = {"model": "alibaba-q38-flash", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://e-dani.com/x.png"}}]}]}
    assert hook._alibaba_multimodal_too_large("alibaba-q38-flash", data) == (False, None)


# ── y lo importante: que se LLAMA, y donde ───────────────────────────────────

def test_el_guard_se_llama_en_el_pre_call_hook_desues_de_resolver_el_alias(hook_src):
    """Un helper que no se invoca es exactamente como el guard de SC-575: verde en
    CI y muertito en produccion. Ademas tiene que ir sobre el alias YA resuelto,
    para cubrir la caida de `tooling`/`q38-*` en la nube, no solo el nombre directo."""
    tree = ast.parse(hook_src)
    hook_fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_pre_call_hook"),
        None,
    )
    assert hook_fn, "no existe async_pre_call_hook"
    calls = [
        (n.lineno, n) for n in ast.walk(hook_fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "_alibaba_multimodal_too_large"
    ]
    assert len(calls) == 1, f"el guard se llama {len(calls)} veces (esperado 1)"
    lineno, call = calls[0]
    resueltos = [
        n.lineno for n in ast.walk(hook_fn)
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "model"
    ]
    assert resueltos and lineno > min(resueltos), (
        "el guard corre antes de resolver el alias: no cubriria la caida a Alibaba"
    )
    # Y el modelo que se le pasa es el resuelto, no el pedido.
    assert any(isinstance(a, ast.Name) and a.id == "model" for a in call.args), (
        "el guard recibe el nombre pedido, no el destino resuelto"
    )
