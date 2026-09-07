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

07-09-2026: `desired_deployments()` era del ConfigMap de
`litellm-dgx-backend-sync`, borrado por llevar muerto desde el 18-08. Los limites
se declaran hoy uno a uno en el `model_list` estatico, que es lo que carga el
proxy, y ahi no hay defaults que heredar: o el alias declara sus numeros o no los
publica. Lo que se fija aqui es lo mismo de siempre, contra la fuente que corre.

Lo que se fija:
  1. los nombres de los campos son los de litellm, y `context_window` no vuelve
  2. ningun limite se hereda: todo alias local declara los suyos
  3. cada residente publica la ventana que sirve de verdad
"""
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# La ventana que sirve de verdad cada checkpoint. Si alguien cambia un
# --max-model-len, este numero y el del model_list tienen que moverse juntos.
DENSO_27B = "qwen38-27b"
QWEN38_FLASH_NEXT = "qwen38-flash-next"
QWEN35_4B = "qwen35-4b"


@pytest.fixture(scope="module")
def config():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if (doc and doc.get("kind") == "ConfigMap"
                and doc["metadata"]["name"] == "litellm-config"):
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


@pytest.fixture(scope="module")
def locales(config):
    """{model_name: entrada} de los alias de CHAT servidos en el cluster."""
    salida = {}
    for entrada in config["model_list"]:
        params = entrada.get("litellm_params") or {}
        info = entrada.get("model_info") or {}
        if ".llm.svc.cluster.local" not in str(params.get("api_base") or ""):
            continue
        if info.get("mode") != "chat":
            continue
        salida[entrada["model_name"]] = entrada
    return salida


def test_el_model_info_usa_los_nombres_de_litellm(config, locales):
    """`context_window` era un campo inventado. Los que litellm lee de verdad son
    `max_input_tokens` y `max_output_tokens`."""
    for nombre, entrada in locales.items():
        info = entrada["model_info"]
        assert "max_input_tokens" in info, nombre
        assert "max_output_tokens" in info, nombre
        assert "context_window" not in info, (
            f"{nombre}: context_window no es un campo de litellm, nada lo lee y "
            "hace invisible la ventana real del modelo")
    assert "context_window" not in yaml.dump(config)


def test_ningun_alias_hereda_sus_limites(locales):
    """Un default en un campo que describe al HARDWARE deja que un alias nuevo
    publique los numeros de otro. Asi es como el 27B anuncio 262144.

    En el model_list estatico esto se cumple por construccion —no hay funcion que
    rellene huecos— y este test lo mantiene asi: un alias sin numeros propios los
    publicaria vacios, no ajenos, pero seguiria mintiendo por omision.
    """
    sin_limites = [
        n for n, e in locales.items()
        if not e["model_info"].get("max_input_tokens")
        or not e["model_info"].get("max_output_tokens")
    ]
    assert not sin_limites, f"alias locales sin limites propios: {sin_limites}"


def test_el_27b_declara_la_ventana_que_sirve(locales):
    info = locales[DENSO_27B]["model_info"]
    assert info["max_input_tokens"] == 262144
    assert info["max_output_tokens"] == 16384


def test_qwen38_flash_next_publica_la_ventana_operativa_de_256k(locales):
    info = locales[QWEN38_FLASH_NEXT]["model_info"]
    assert info["max_input_tokens"] == 262144
    assert info["max_output_tokens"] == 16384


def test_qwen35_4b_publica_el_contexto_real_de_llama_cpp(locales):
    info = locales[QWEN35_4B]["model_info"]
    assert info["max_input_tokens"] == 32768
    assert info["max_output_tokens"] == 8192


def test_qwen38_flash_next_publica_su_nombre_directo_solo_en_su_backend(locales):
    """El nombre directo de un modelo no puede resolver a otro checkpoint.

    `tooling` es el alias de CAPACIDAD y puede cambiar de dueño con el perfil;
    `qwen38-flash-next` nombra un modelo concreto y tiene que ir a ese servidor o
    fallar en duro.
    """
    directo = locales[QWEN38_FLASH_NEXT]["litellm_params"]["api_base"]
    assert "qwen38-flash-next.llm.svc.cluster.local" in directo
    otros = {
        n: e["litellm_params"]["api_base"]
        for n, e in locales.items()
        if n.startswith("qwen38-flash-next")
    }
    assert all("qwen38-flash-next" in base for base in otros.values()), otros


def test_los_nombres_dense_retirados_no_vuelven(config):
    """`dense`, `dense-reasoning`, `dense-uncensored` y `taxonomy` se retiraron el
    15-08 tras migrar sus consumidores. Eran alias del 27B; hoy ese backend se
    sirve por `tooling` (capacidad) y `qwen38-27b` (nombre directo)."""
    publicados = {e["model_name"] for e in config["model_list"]}
    for muerto in ("dense", "dense-reasoning", "dense-uncensored", "taxonomy"):
        assert muerto not in publicados, f"{muerto} volvio al model_list"
    assert {"tooling", "qwen38-27b"} <= publicados
