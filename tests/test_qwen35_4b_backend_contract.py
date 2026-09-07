"""Contract for the small Qwen3.5 llama.cpp backend on the x86 RTX.

07-09-2026: este fichero leia el `BACKENDS` del ConfigMap de
`litellm-dgx-backend-sync`, borrado del repo por llevar muerto desde el 18-08. Lo
que se comprobaba sigue comprobandose, ahora contra el `model_list` estatico, que
es lo que carga el proxy. Cae con el controlador `id_prefix` (era el prefijo del
ID que registraba por /model/new: sin registro no hay ID) y cae el test del
reconciliador de la key de OpenClaw, cuyo allowlist se vacio a proposito el 18-08
—vacio = todos— porque ensanchandolo habia acumulado 48 nombres, ~12 de modelos
inexistentes, y a la vez daba 403 a los alias nuevos.
"""

from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _config() -> dict:
    for document in yaml.safe_load_all(MANIFEST.read_text()):
        if (document and document.get("kind") == "ConfigMap"
                and document["metadata"]["name"] == "litellm-config"):
            return yaml.safe_load(document["data"]["config.yaml"])
    raise AssertionError("litellm-config ConfigMap not found")


def _qwen_entry() -> dict:
    for entry in _config()["model_list"]:
        if entry["model_name"] == "qwen35-4b":
            return entry
    raise AssertionError("qwen35-4b missing from model_list")


def test_qwen_backend_uses_its_ready_clusterip_and_one_slot():
    entry = _qwen_entry()
    params = entry["litellm_params"]
    info = entry["model_info"]
    assert info["backend"] == "rtx"
    assert params["api_base"] == "http://qwen35-4b-int4.llm.svc.cluster.local:8000/v1"
    assert params["max_parallel_requests"] == 1
    assert info["max_input_tokens"] == 32768
    assert info["max_output_tokens"] == 8192


def test_qwen_backend_exposes_only_verified_capabilities():
    info = _qwen_entry()["model_info"]
    assert info["supports_function_calling"] is True
    # 2026-08-15: era False. El test pide capacidades VERIFICADAS, y esta se
    # verifico contra el motor vivo: con reasoning_effort=high devuelve 1231 chars
    # de reasoning_content y con max 1405; none/low/medium no piensan. Declararlo
    # False mientras el modelo si razonaba es lo que hacia que los clientes que
    # leen /model/info nunca ofrecieran la opcion.
    assert info["supports_reasoning"] is True
    # 2026-08-19: `low` y `medium` fuera. El propio comentario de arriba ya decia
    # que no piensan, y el hook tampoco los traduce: un nivel anunciado y no
    # honrado deja al cliente parado en el creyendo que piensa.
    assert list(info["supported_reasoning_efforts"]) == ["none", "high", "max"]
    assert info["supports_vision"] is False


def test_qwen_aliases_are_specific_and_do_not_take_over_fast():
    """`qwen3.5-4b` existe solo como compatibilidad, y `fast` no es de nadie.

    El nombre puntuado se sirve por `model_group_alias` con `hidden: true`, que lo
    deja fuera de /v1/models —de donde OpenClaw siembra el menu— y el router lo
    sigue resolviendo al grupo. Y `fast` no se publica: fue un alias generico que
    se retiro para que un modelo pequeno no se quede con el nombre.
    """
    config = _config()
    publicados = {entry["model_name"] for entry in config["model_list"]}
    assert "qwen35-4b" in publicados
    assert "qwen3.5-4b" not in publicados
    assert "fast" not in publicados

    grupo = config["router_settings"]["model_group_alias"]["qwen3.5-4b"]
    assert grupo["model"] == "qwen35-4b"
    assert grupo["hidden"] is True
