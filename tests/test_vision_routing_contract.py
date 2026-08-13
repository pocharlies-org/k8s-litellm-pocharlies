"""Contrato del desvio de VISION (2026-08-11).

DeepSeek es solo texto. Mientras sea el residente, una peticion con imagen a un
nombre de CAPACIDAD se manda al unico modelo del gateway que ve: la cuenta
ChatGPT Pro por codex-bridge (`gpt-5.6-luna`).

Lo que este contrato fija, que es lo que se puede romper sin darse cuenta:

  - sin imagen no se desvia nunca, aunque el backend no vea;
  - un backend que declara que VE se queda la peticion (no se gasta cuota);
  - lo que NO declara que ve se desvia, incluido el caso "no hay nada
    registrado". Ese es el que se midio en produccion el 2026-08-11: con el plano
    local en transicion no queda ningun alias, y el criterio anterior (desviar
    solo con un False explicito) dejaba la vision rota justo entonces;
  - un nombre de MODELO solo se desvia si SABEMOS que no ve. Con "no se sabe" se
    respeta el nombre: las mediciones se hacen contra esos nombres.

Se carga el hook REAL desde el manifest y se ejecutan solo sus funciones puras,
inyectando las dos sondas que tocan internals de litellm.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

WANT_FN = {"_vision_target", "_has_part_type", "_message_entries",
           # sondas por defecto: tienen que existir para que la firma de
           # _vision_target ligue en el `def`. Los tests inyectan stubs.
           "_alias_supports_vision", "_alias_has_deployments"}
WANT_CONST = {"VISION_FALLBACK_MODEL", "VISION_DIVERTIBLE", "AUTO_ROUTED_MODELS",
              "CAPABILITY_CHAINS", "IMAGE_PART_TYPES"}


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
    mod = types.ModuleType("hookvision")
    mod.__dict__["os"] = __import__("os")
    mod.__dict__["log"] = __import__("logging").getLogger("test")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)
    return mod


IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def _con_imagen():
    return {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "que ves?"}, IMG]}]}


def _sin_imagen():
    return {"messages": [{"role": "user", "content": "que ves?"}]}


def _target(hook, data, requested, served, ve=False, sol_viva=True):
    return hook._vision_target(
        data, requested, "", served,
        supports_vision=lambda alias: ve,
        alias_live=lambda alias: sol_viva,
    )


def test_capacidad_con_imagen_sobre_backend_ciego_se_desvia(hook):
    for alias in sorted(hook.VISION_DIVERTIBLE):
        assert _target(hook, _con_imagen(), alias, "tooling") == hook.VISION_FALLBACK_MODEL, alias


def test_capacidad_con_imagen_y_registro_VACIO_se_desvia(hook):
    """EL CASO MEDIDO (2026-08-11): plano local en transicion, cero alias vivos.

    `get_model_list("tooling")` devuelve vacio -> supports_vision es None. Antes
    eso NO desviaba y la peticion moria con 503 de la admision de compute-mode.
    Es justo el momento en que el desvio es la unica forma de contestar.
    """
    assert hook._vision_target(
        _con_imagen(), "tooling", "", "tooling",
        supports_vision=lambda alias: None,
        alias_live=lambda alias: True,
    ) == hook.VISION_FALLBACK_MODEL


def test_nombre_de_modelo_sin_veredicto_no_se_desvia(hook):
    """Un nombre de MODELO que no dice si ve se sirve tal cual.

    Si esto cae, una medida contra `ornith-1.0` puede acabar respondida por
    ChatGPT sin que nadie lo pida.
    """
    for alias in ("ornith-1.0", "dense-uncensored", "qwen36-35b"):
        assert hook._vision_target(
            _con_imagen(), alias, "", alias,
            supports_vision=lambda a: None,
            alias_live=lambda a: True,
        ) is None, alias


def test_nombre_de_modelo_que_declara_que_NO_ve_si_se_desvia(hook):
    """Sabemos que iba a fallar seguro: desviar no puede estropear ninguna medida."""
    assert _target(hook, _con_imagen(), "deepseek-v4-flash-0731", "deepseek-v4-flash-0731",
                   ve=False) == hook.VISION_FALLBACK_MODEL


def test_sin_imagen_no_se_desvia(hook):
    assert _target(hook, _sin_imagen(), "tooling", "tooling") is None
    assert _target(hook, _sin_imagen(), "tooling", "tooling", ve=None) is None


def test_backend_que_ve_se_queda_la_peticion(hook):
    """Un residente multimodal (Ornith, el 27B, el nvidia-qwen36) recupera lo suyo
    sin tocar nada: el desvio se apaga solo."""
    assert _target(hook, _con_imagen(), "router", "dense", ve=True) is None
    for alias in sorted(hook.VISION_DIVERTIBLE):
        assert _target(hook, _con_imagen(), alias, "tooling", ve=True) is None, alias


def test_no_se_desvia_si_el_modelo_de_vision_no_esta_registrado(hook):
    assert _target(hook, _con_imagen(), "router", "tooling", sol_viva=False) is None


def test_no_se_desvia_a_si_mismo(hook):
    """Pedir el propio modelo de vision no puede reentrar en el desvio."""
    assert _target(hook, _con_imagen(), "router", hook.VISION_FALLBACK_MODEL) is None


def test_el_destino_es_un_modelo_del_model_list(hook):
    """El desvio apunta a una entrada estatica del config, no a un alias inventado."""
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cfg = next(d["data"]["config.yaml"] for d in docs
               if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    entradas = {m["model_name"]: m for m in yaml.safe_load(cfg)["model_list"]}
    destino = entradas.get(hook.VISION_FALLBACK_MODEL)
    assert destino, f"{hook.VISION_FALLBACK_MODEL} no esta en model_list"
    assert destino["model_info"].get("supports_vision") is True, (
        "el destino del desvio tiene que declarar supports_vision; si no, "
        "_alias_supports_vision lo lee como 'no se sabe'")


def test_las_partes_de_imagen_de_la_responses_api_tambien_cuentan(hook):
    """`input_image` (Responses) vale igual que `image_url` (chat/completions)."""
    data = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "que ves?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]}]}
    assert _target(hook, data, "tooling", "tooling") == hook.VISION_FALLBACK_MODEL
