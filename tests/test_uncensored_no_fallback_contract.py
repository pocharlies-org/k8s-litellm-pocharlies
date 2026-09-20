"""La ruta abliterada no puede degradar a un modelo censurado.

Quien pide `qwen38-flash-next-uncensored` (o el alias de capacidad
`tooling-uncensored`) está pidiendo explícitamente que NO haya rechazo. Si el
residente del Spark está saturado o caído, degradar a `alibaba-q38-flash`
devolvería el modelo BASE de Alibaba: el fallback no hereda el `cache_salt`,
porque `refusal:N` es una extensión de NUESTRO vLLM y Alibaba no la conoce. El
resultado sería una petición que pidió «sin rechazo» contestada por un modelo
que rechaza, sin avisar. Un fallo visible es la respuesta correcta.

Dos capas, y este contrato cubre las dos:

  1. el mapa `router_settings.fallbacks` — hoy solo tiene `qwen38-flash-next`, y
     el Router de LiteLLM resuelve por nombre de grupo EXACTO
     (`item.keys()[0] == model_group`), así que el grupo `-uncensored` no cae a
     ninguna parte;
  2. `_apply_uncensored_fallback_policy`, que estampa `disable_fallbacks` en la
     petición. El Router lo consume ANTES de mirar el mapa
     (`async_function_with_fallbacks_common_utils`: `if disable_fallbacks is
     True: raise e`).

La 2 es la que importa a largo plazo: la 1 se rompe sola el día que alguien añada
una entrada para el grupo abliterado o un `default_fallbacks` con `"*"`, que se
aplica a TODOS los grupos sin preguntar. Y el stamp va SOBRESCRITO, no
`setdefault`, para que un body del cliente con `disable_fallbacks: false` no
pueda quitar la política.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# Nombres que sirven la ruta abliterada. Se lista a mano A PROPOSITO: si alguien
# añade un `-uncensored` nuevo al model_list sin declararlo aquí, el primer test
# ruge. Es el mismo truco que `MODEL_SCOPED_LAMBDA` en
# test_uncensored_alias_contract.py.
UNCENSORED_NAMES = (
    "tooling-uncensored",
    "qwen38-flash-next-uncensored",
    "qwen38-27b-uncensored",
)

# Nombres censurados que SÍ llevan fallback declarado. El stamp no debe tocarlos:
# degradar ahí es la intención declarada (mismo modelo, otra GPU).
CENSURABLES = ("qwen38-flash-next", "tooling")

WANT_FN = {"_apply_uncensored_fallback_policy"}
WANT_CONST = {
    "UNCENSORED_GATED_ALIASES",
    "TOOLING_UNCENSORED_ALIASES",
    "TOOLING_UNCENSORED_MODE_TARGETS",
}


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in _docs()
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def hook():
    """El hook real, solo sus piezas puras. Sin importar litellm."""
    tree = ast.parse(_hook_source())
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets)
        )
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("hookpure")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


@pytest.fixture(scope="module")
def config():
    for doc in _docs():
        data = doc.get("data") or {}
        if doc.get("kind") == "ConfigMap" and "config.yaml" in data:
            return yaml.safe_load(data["config.yaml"])
    raise AssertionError("no encuentro el config.yaml de LiteLLM")


@pytest.fixture(scope="module")
def model_list(config):
    return {m["model_name"]: m for m in config["model_list"]}


# ── capa 1: el mapa de fallbacks no alcanza la ruta abliterada ──────────────────

def test_ningun_grupo_abliterado_tiene_entrada_en_el_mapa(config):
    """Y ningún `"*"`: `default_fallbacks` se aplica a TODOS los grupos."""
    fallbacks = (config.get("router_settings") or {}).get("fallbacks") or []
    for entry in fallbacks:
        for grupo, destinos in entry.items():
            assert grupo not in UNCENSORED_NAMES, (
                f"`fallbacks:` da salida a {grupo!r} -> {destinos}. El fallback no "
                "hereda el `cache_salt`: la petición abliterada acabaría en el "
                "modelo censurado de Alibaba sin avisar."
            )
            assert grupo != "*", (
                "un fallback por defecto (`*`) cubre también los grupos "
                "abliterados, que es justo lo que no puede pasar"
            )


def test_todos_los_nombres_abliterados_del_model_list_estan_declarados(model_list):
    """Un `-uncensored` nuevo sin añadir a UNCENSORED_NAMES deja de estar cubierto."""
    servidos = {
        name for name in model_list
        if name.endswith("-uncensored") or name in UNCENSORED_NAMES
    }
    assert servidos == set(UNCENSORED_NAMES), sorted(servidos ^ set(UNCENSORED_NAMES))


# ── capa 2: el seguro, que no depende del mapa ─────────────────────────────────

def test_cada_nombre_abliterado_va_en_el_gate(hook):
    """El stamp se dispara por pertenencia a UNCENSORED_GATED_ALIASES."""
    for name in UNCENSORED_NAMES:
        assert name in hook.UNCENSORED_GATED_ALIASES, name


@pytest.mark.parametrize("name", UNCENSORED_NAMES)
def test_pedir_abliterado_estampa_disable_fallbacks(hook, name):
    """Tanto por el nombre PEDIDO (nombre directo) como por el RESUELTO.

    `tooling-uncensored` llega como nombre pedido; `qwen38-flash-next-uncensored`
    puede llegar pedido o escrito por el hook tras resolver el perfil vivo. Las
    dos vias se comprueban.
    """
    pedido = {"model": name}
    assert hook._apply_uncensored_fallback_policy(pedido, name, "tooling") is True
    assert pedido["disable_fallbacks"] is True

    resuelto = {"model": name}
    assert hook._apply_uncensored_fallback_policy(resuelto, "tooling-uncensored", name) is True
    assert resuelto["disable_fallbacks"] is True


@pytest.mark.parametrize("name", CENSURABLES)
def test_una_ruta_censurada_no_se_toca(hook, name):
    """Degradar `qwen38-flash-next` a su gemelo en Alibaba es lo declarado."""
    data = {"model": name}
    assert hook._apply_uncensored_fallback_policy(data, name, name) is False
    assert "disable_fallbacks" not in data, name


def test_el_body_del_cliente_no_puede_quitar_la_politica(hook):
    """Sobrescribe, no `setdefault`.

    Un `disable_fallbacks: false` en el body llega en `data` antes del hook. Con
    `setdefault` la política se la saltaba un cliente que la pidiera.
    """
    data = {"model": "qwen38-flash-next-uncensored", "disable_fallbacks": False}
    assert hook._apply_uncensored_fallback_policy(
        data, "qwen38-flash-next-uncensored", "qwen38-flash-next-uncensored"
    ) is True
    assert data["disable_fallbacks"] is True


def test_el_stamp_esta_almbrado_en_el_hook(hook):
    """La función existir no basta: tiene que correr en el pre-call hook.

    El 19-08 se dejó muerta la rama de `tooling-uncensored` por no estar el alias
    en `CAPABILITY_CHAINS` (ver el comentario de allí). Mismo modo de fallo: una
    política que no se llama es una política que no existe.
    """
    tree = ast.parse(_hook_source())
    clases = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef)
    }
    gancho = next(
        (
            m
            for m in ast.walk(clases["StripUnsupportedParams"])
            if isinstance(m, ast.AsyncFunctionDef) and m.name == "async_pre_call_hook"
        ),
        None,
    )
    assert gancho is not None, "StripUnsupportedParams.async_pre_call_hook ha cambiado"
    llamados = {
        node.func.id
        for node in ast.walk(gancho)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_apply_uncensored_fallback_policy" in llamados, sorted(llamados)
