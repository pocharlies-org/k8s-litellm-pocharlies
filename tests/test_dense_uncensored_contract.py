"""Quien es dueno de cada alias local, y contra que servicio resuelve.

07-09-2026: este fichero leia el `BACKENDS` del ConfigMap de
`litellm-dgx-backend-sync` — id_prefix, RETIRED_MANAGED_ID_PREFIXES,
conflicting_managed_ids, el orden de expulsion dentro de reconcile_backend. Todo
eso describia el registro por HTTP contra /model/new, que dejo de existir el
18-08 y se borro del repo hoy. Las invariantes que quedan son las que siguen
siendo ciertas sin controlador, expresadas contra el `model_list` estatico.

En particular, la unicidad de `tooling` ya no la impone nadie a base de expulsar
registros: `tooling` es UNA entrada que apunta al Service de pool de Kubernetes, y
quien esta detras lo decide el perfil activo. La exclusion es fisica, no de
config.
"""
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _texto() -> str:
    return MANIFEST.read_text()


def _bloque(text, start, end):
    return text[text.index(start):text.index(end)]


def _config() -> dict:
    for doc in yaml.safe_load_all(_texto()):
        if (doc and doc.get("kind") == "ConfigMap"
                and doc["metadata"]["name"] == "litellm-config"):
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def _entradas() -> dict[str, dict]:
    return {e["model_name"]: e for e in _config()["model_list"]}


def test_uncensored_is_the_creative_backend_owning_tooling_and_dense_aliases():
    """Desde que se borraron los deployments censurados del 27B (2026-07-26), el
    27B abliterado es el UNICO modelo denso del cluster, y por eso se queda con
    sus dos alias: `tooling` (capacidad) y `qwen38-27b` (modelo concreto). Los
    cuatro dense-shaped (`dense`, `dense-reasoning`, `dense-uncensored`,
    `taxonomy`) se retiraron el 15-08 tras migrar sus consumidores."""
    entradas = _entradas()

    # El nombre directo va SIEMPRE al servidor del 27B.
    directo = entradas["qwen38-27b"]["litellm_params"]["api_base"]
    assert "vllm-qwen38-27b-uncensored.llm.svc.cluster.local" in directo

    # El alias de capacidad va al Service de POOL: quien esta detras lo decide el
    # perfil activo, no este fichero.
    for capacidad in ("tooling", "tooling-uncensored"):
        base = entradas[capacidad]["litellm_params"]["api_base"]
        assert "tooling.llm.svc.cluster.local" in base, capacidad

    for muerto in ("dense", "dense-reasoning", "dense-uncensored", "taxonomy"):
        assert muerto not in entradas, f"{muerto} volvio al model_list"


def test_qwen_direct_name_is_not_a_capability_alias():
    """Un nombre concreto falla si su backend cae; no se cambia silenciosamente."""
    text = _texto()
    capabilities = _bloque(text, "CAPABILITY_CHAINS = {", "# ── Desvio de VISION")
    assert '"qwen38-27b"' not in capabilities
    assert '"dense"' not in capabilities


def test_los_tres_backends_locales_y_sus_formas_de_exclusion():
    """3 backends locales; los dos Spark conservan su exclusion y el RTX es independiente.

    Actualizado 2026-08-13 (ventana RHO backend-sync): eran 4. Se retiraron
    `ornith-dgx1` y `nvidia-qwen36-dgx1`, los dos candidatos al asiento de DGX1:
    estaban a replicas 0 Y SIN PESOS EN DISCO (Ornith borrado el 10-08; la carpeta
    nvidia-qwen36-35b-a3b-nvfp4 no existe en dgx1), asi que ninguno podia arrancar.
    El asiento en si caduco el 08-08, cuando el residente TP=2 paso a ocupar los
    DOS Sparks: mientras corre no cabe residente en DGX1, no por politica sino por
    memoria — y por eso el residente SI comparte los alias de tooling.
    """
    entradas = _entradas()
    por_backend = {
        n: e["model_info"]["backend"]
        for n, e in entradas.items()
        if ".llm.svc.cluster.local" in str((e.get("litellm_params") or {}).get("api_base") or "")
        and (e.get("model_info") or {}).get("mode") == "chat"
    }
    assert set(por_backend.values()) == {"dgx1", "dgx1+dgx2", "rtx", "profile-resident"}
    assert por_backend["qwen38-27b"] == "dgx1"
    assert por_backend["qwen38-flash-next"] == "dgx1+dgx2"
    assert por_backend["qwen35-4b"] == "rtx"
    # El alias de capacidad no nombra un nodo a proposito: lo resuelve el perfil.
    assert por_backend["tooling"] == "profile-resident"

    texto = _texto()
    for dead in ("gemma-dgx1", "qwen36-35b-dgx1", "qwen36-35b-dgx2",
                 "qwen36-27b-dense-dgx1", "qwen36-27b-dense-dgx2",
                 # retirado del cluster entero el 2026-08-10
                 "qwen3coder-dgx2", "qwen3coder-dgx1",
                 # 2026-08-13: sin pesos en disco, no podian arrancar
                 "ornith-dgx1", "nvidia-qwen36-dgx1"):
        assert f'"{dead}"' not in texto, f"{dead} volvio al manifiesto"


def test_tooling_tiene_un_solo_dueno_y_lo_resuelve_kubernetes():
    """LiteLLM no impone unicidad de alias: dos entradas sobre `tooling` harian que
    el router balanceara contra un api_base muerto.

    Hasta el 18-08 esto lo garantizaba el controlador expulsando registros ajenos
    antes de dar de alta el entrante. Hoy se garantiza por construccion: hay UNA
    entrada, y a quien sirve lo decide el Service de pool `tooling` — que
    selecciona por la etiqueta `llm.dgx-infra/pool: tooling-resident` — segun el
    perfil activo. Sin endpoints el alias no responde, que es el fallo VISIBLE que
    se buscaba.
    """
    nombres = [e["model_name"] for e in _config()["model_list"]]
    assert nombres.count("tooling") == 1, "hay dos dueños de `tooling` en el model_list"
    assert nombres.count("tooling-uncensored") == 1
    # Y los alias del coder retirado no vuelven por la puerta de atras.
    assert "QWEN3CODER_ALIASES" not in _texto()


def test_manifest_stays_valid_yaml():
    list(yaml.safe_load_all(_texto()))
