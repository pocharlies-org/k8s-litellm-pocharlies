"""Contrato de los limites de contexto y salida que se publican en LiteLLM.

Este fichero existe por un fallo que duro meses sin que nadie lo viera: los
backends declaraban su ventana como `model_info.context_window`, que **no es un
campo de litellm** (`'context_window' in get_type_hints(ModelInfo)` -> False). Se
guardaba como metadato suelto, no lo leia nadie, y el unico numero que veia un
cliente era `max_tokens: 16384` — que es el techo de SALIDA. Consecuencias reales:
opencode configurado con 16k de contexto en vez de 256k, y el catalogo de Aurora
con `contextLength:"256K"` hardcodeado en un parche del bundle del frontend porque
la API no lo exponia.

Y el 27B denso no declaraba NINGUNO de los dos limites, asi que heredaba los
defaults de `desired_deployments()` y anunciaba 262144 cuando sirve 229376: 32k mas
de los que puede.

Lo que se fija aqui:
  1. los nombres de los campos son los de litellm, y `context_window` no vuelve
  2. ningun limite se hereda: todo backend declara los suyos
  3. el router automatico ya no depende del 27B ni necesita un escape especial
"""
import ast
import os
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# La ventana que sirve de verdad cada checkpoint. Si alguien cambia un
# --max-model-len, este numero y el del sync tienen que moverse juntos.
DGX2_UNCENSORED_27B = "qwen36-27b-uncensored-dgx2"
DEEPSEEK_V4_FLASH = "deepseek-v4-flash-tp2"


@pytest.fixture(scope="module")
def cms():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]

    def data(name, key):
        return next(d["data"][key] for d in docs
                    if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == name)

    return {
        "sync": data("litellm-dgx-backend-sync", "sync.py"),
        "hook": data("litellm-config", "litellm_strip_params.py"),
    }


@pytest.fixture(scope="module")
def backends(cms):
    """BACKENDS del sync, evaluado de verdad (no por regex sobre el texto)."""
    tree = ast.parse(cms["sync"])
    wanted = {"ORNITH_CANARY_ALIASES", "QWEN36_COMPAT_ALIASES", "ORNITH_ALIASES",
              "THINKING_TIER_ALIASES",
              "TOOLING_RESIDENT_ALIASES", "DEEPSEEK_V4_FLASH_DIRECT_ALIASES",
              "QWEN3CODER_ALIASES",
              "QWEN36_27B_UNCENSORED_ALIASES", "QWEN36_REPEAT_GUARD_PARAMS",
              "BACKENDS"}
    keep = [n for n in tree.body
            if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in wanted
                                                 for t in n.targets)]
    mod = types.ModuleType("syncpure")
    mod.os = os
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<sync>", "exec"), mod.__dict__)
    return {b["name"]: b for b in mod.BACKENDS}


def _const(src, name):
    """Valor de una constante de modulo, sin ejecutar el resto del fichero."""
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} ya no se define")


def test_el_model_info_usa_los_nombres_de_litellm(cms):
    """`context_window` era un campo inventado. Los que litellm lee de verdad son
    `max_input_tokens` y `max_output_tokens`; `max_tokens` es el legacy y se
    mantiene con el valor de salida, que es la convencion que aplica litellm a los
    modelos que si conoce (gpt-5.6-sol: max_tokens == max_output_tokens)."""
    block = cms["sync"]
    block = block[block.index("def desired_deployments"):block.index("def current_model_ids")]
    for field in ('"max_input_tokens": backend["max_input_tokens"]',
                  '"max_output_tokens": backend["max_output_tokens"]',
                  '"max_tokens": backend["max_output_tokens"]'):
        assert field in block, f"falta {field} en el model_info publicado"
    assert '"context_window"' not in block, (
        "context_window no es un campo de litellm: nada lo lee y hace invisible la "
        "ventana real del modelo")


def test_ningun_backend_hereda_sus_limites(cms, backends):
    """Un default en un campo que describe al HARDWARE deja que un backend nuevo
    publique los numeros de otro. Asi es como el 27B anuncio 262144."""
    block = cms["sync"]
    block = block[block.index("def desired_deployments"):block.index("def current_model_ids")]
    assert 'backend.get("max_tokens"' not in block
    assert 'backend.get("context_window"' not in block
    assert 'backend.get("max_input_tokens"' not in block
    assert 'backend.get("max_output_tokens"' not in block

    for name, b in backends.items():
        for field in ("max_input_tokens", "max_output_tokens"):
            assert b.get(field), f"{name} no declara {field}"

    # Y que el guard de arranque exista: sin el, olvidarse de un limite en un
    # backend nuevo revienta con KeyError a mitad de un ciclo en vez de morir al
    # importar.
    assert 'REQUIRED_BACKEND_LIMITS = ("max_input_tokens", "max_output_tokens")' in cms["sync"]
    assert "raise SystemExit" in cms["sync"]


def test_el_27b_declara_su_ventana_mas_estrecha(backends):
    """El catalogo debe seguir publicando la ventana real del 27B.

    El router automatico ya no lo usa ni depende de este limite.
    """
    estrechos = {n: b["max_input_tokens"] for n, b in backends.items()
                 if b["max_input_tokens"] < 262144}
    assert estrechos == {DGX2_UNCENSORED_27B: 65536}, estrechos


def test_deepseek_publica_la_ventana_operativa_de_384k(backends):
    """El catalogo solo puede anunciar el max_model_len que sirve vLLM."""
    assert backends[DEEPSEEK_V4_FLASH]["max_input_tokens"] == 393216
    assert backends[DEEPSEEK_V4_FLASH]["max_output_tokens"] == 16384


def test_deepseek_publica_su_nombre_directo_solo_en_su_backend(backends):
    """El nombre concreto no puede resolver silenciosamente a otro residente."""
    alias = "deepseek-v4-flash-0731"
    owners = [name for name, backend in backends.items() if alias in backend["aliases"]]
    assert owners == [DEEPSEEK_V4_FLASH]


def test_el_reconcile_refresca_metadatos_aunque_el_id_sea_estable(cms):
    """Cambiar 256K -> 384K debe reemplazar el registro ya existente."""
    tree = ast.parse(cms["sync"])
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "managed_model_contract"
    )
    namespace = {"Any": object}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<sync>", "exec"), namespace)
    contract = namespace["managed_model_contract"]
    current = {
        "model_name": "agent",
        "litellm_params": {
            "model": "openai/deepseek-v4-flash-0731",
            "api_base": "http://deepseek/v1",
            "max_parallel_requests": 6,
        },
        "model_info": {
            "max_tokens": 16384,
            "max_output_tokens": 16384,
            "max_input_tokens": 262144,
            "supports_function_calling": True,
            "supports_vision": False,
            "backend": "dgx1+dgx2",
            "k8s_namespace": "llm",
            "k8s_service": "deepseek-v4-flash-0731",
        },
    }
    desired = {**current, "model_info": {**current["model_info"], "max_input_tokens": 393216}}
    assert contract(current) != contract(desired)

    reconcile = cms["sync"]
    reconcile = reconcile[reconcile.index("def reconcile_backend"):reconcile.index("def main()")]
    assert 'log.info("refreshing changed model contract %s", model_id)' in reconcile
    assert "delete_model(model_id)" in reconcile


def test_router_automatico_no_depende_del_dense(cms):
    """Los limites del 27B no pueden volver a condicionar el perfil automatico."""
    tree = ast.parse(cms["hook"])
    route = _const(cms["hook"], "ROUTE")
    assert {entry["model"] for entry in route.values()} == {
        "tooling", "agent", "high", "max"}
    assert all(not alias.startswith("dense")
               for entry in route.values()
               for alias in (entry["model"], *entry.get("fallbacks", ())))
    assert not any(isinstance(node, ast.Assign) and any(
        getattr(target, "id", "") == "DENSE_CTX_ESCAPE" for target in node.targets)
        for node in tree.body)
