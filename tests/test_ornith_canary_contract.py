"""Ornith esta RETIRADO. Este test guarda la retirada, no el backend.

HISTORIA (2026-08-13, ventana RHO backend-sync)
-----------------------------------------------
Este fichero afirmaba que `ornith-dgx1` era el unico backend de DGX1 y dueño de
el perfil local mas sus dos nombres de canary. Dejo de ser cierto por
partes y en fechas distintas:

  - 10-08-2026: se BORRAN los pesos de Ornith del disco por decision del operador.
  - 08-08-2026: el residente TP=2 pasa a ocupar los DOS Sparks, con lo que el
    "asiento de residente de DGX1" deja de existir: mientras corra no cabe nadie
    en DGX1, no por politica sino por memoria.
  - 13-08-2026: se retiran `ornith-dgx1` y `nvidia-qwen36-dgx1`, los dos
    candidatos a ese asiento. Ninguno tenia pesos en disco (la carpeta
    nvidia-qwen36-35b-a3b-nvfp4 tampoco existe en dgx1), asi que declararlos era
    describir un mundo que ya no esta.

El test antiguo tenia ademas un historial de asserts rancios: estuvo ROJO desde
antes del 27-07 sin que nadie lo viera, "porque CI solo corre el contrato de red".
Reescribirlo para que verifique la retirada es mas util que borrarlo: si alguien
reintroduce el backend sin reponer los pesos, esto lo cuenta.

07-09-2026: la retirada se comprobaba sobre el `BACKENDS` del ConfigMap de
`litellm-dgx-backend-sync`, borrado hoy por llevar muerto desde el 18-08. Se
comprueba ahora sobre el `model_list` estatico, que es lo unico que publica
alias. La guarda es MAS fuerte que antes: entonces bastaba con no estar en
BACKENDS, ahora tiene que no estar publicado en ningun sitio.

CONSECUENCIA ABIERTA, deliberadamente NO cubierta aqui: `ornith-1.0` y
`ornith-canary` dejan de ser alias servibles. Ya fallaban antes de esta retirada
—nadie los servia—, igual que `dense`, `dense-reasoning`, `dense-uncensored` y
`taxonomy`. Que hacer con esos seis nombres huerfanos es una decision de
servicio, no de config.
"""
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _config() -> dict:
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if (doc and doc.get("kind") == "ConfigMap"
                and doc["metadata"]["name"] == "litellm-config"):
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def test_ornith_no_vuelve_sin_pesos():
    texto = MANIFEST.read_text()
    for muerto in ("ornith-dgx1", "nvidia-qwen36-dgx1"):
        assert muerto not in texto, (
            f"{muerto} volvio al manifiesto. Se retiro el 2026-08-13 por no tener "
            "pesos en disco: comprueba que el checkpoint existe ANTES de "
            "reintroducirlo, o se publicara un alias que no puede responder."
        )


def test_los_alias_de_ornith_ya_no_los_declara_nadie():
    """`ornith-1.0` / `ornith-canary` nombran un MODELO concreto.

    Su regla original sigue siendo la correcta: quien pide un nombre de modelo
    debe recibir ese modelo o un error visible, nunca la respuesta de otro. Con
    Ornith retirado, lo correcto es que nadie los publique — que fallen en duro —
    y no que se los quede el residente de turno en silencio.
    """
    config = _config()
    publicados = {e["model_name"] for e in config["model_list"]}
    grupos = set(config.get("router_settings", {}).get("model_group_alias") or {})
    for alias in ("ornith-canary", "ornith-1.0"):
        assert alias not in publicados, (
            f"{alias} esta en el model_list. Es un nombre de MODELO: si lo sirve "
            "otro checkpoint, quien lo pide recibe algo distinto de lo que pidio "
            "sin enterarse."
        )
        assert alias not in grupos, f"{alias} volvio como model_group_alias"


def test_los_alias_de_capacidad_siguen_teniendo_dueno():
    """La retirada no puede dejar `tooling` y compania sin backend publicado.

    26-08: el asiento pasa a qwen38-flash-next (residente llm-tp). Su TP=2 excluye
    a cualquier otro por hardware en los dos nodos, asi que el alias de capacidad
    no nombra el checkpoint: apunta al Service de pool `tooling`, y quien esta
    detras lo decide el perfil activo.
    """
    entradas = {e["model_name"]: e for e in _config()["model_list"]}
    for capacidad in ("tooling", "tooling-uncensored"):
        assert capacidad in entradas, f"{capacidad} se quedo sin dueño publicado"
        base = entradas[capacidad]["litellm_params"]["api_base"]
        assert "tooling.llm.svc.cluster.local" in base, (
            f"{capacidad} apunta a {base}: el alias de capacidad tiene que ir al "
            "Service de pool, no a un checkpoint concreto"
        )
    # Y el residente llm-tp vivo sigue publicando su nombre directo.
    assert "qwen38-flash-next" in entradas
