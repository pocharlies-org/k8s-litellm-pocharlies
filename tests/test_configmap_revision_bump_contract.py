"""Cambiar sync.py obliga a bumpear la anotacion que reinicia su pod.

POR QUE EXISTE (2026-08-13, ventana RHO backend-sync)
-----------------------------------------------------
El commit 7f8634f retiro cinco alias `qwen36-35b*` del sync. ArgoCD lo aplico y
dijo `Synced / Healthy`. Kubelet refresco el ConfigMap dentro del pod a las 11:08
(se ve en el symlink `/app/..data -> ..2026_08_13_11_08_07...`). Y aun asi los
cinco alias siguieron registrados en LiteLLM DOCE HORAS.

Causa: `main()` del sync es un `while True` + `time.sleep()` sin `importlib.reload`
ni watcher. Python importa el modulo al arrancar y NO lo relee nunca. El fichero
en disco estaba bien; el proceso llevaba desde las 01:37 con el codigo viejo.

`Synced / Healthy` significa "el manifiesto aplicado coincide con git", NO "el
proceso ejecuta ese codigo". La distincion cuesta un dia de depuracion cuando el
sintoma es "el cambio no hace nada" y no hay ningun error en ningun log.

La convencion para forzar el rollout ya existia — la anotacion
`config.k8s.e-dani.com/revision`, con el comentario "The ConfigMap alone does not
restart this pod" al lado — pero dependia de que quien tocara el codigo se
acordara. No sirve: quien hizo el cambio de 7f8634f no se acordo.

Este test lo convierte en mecanico. La anotacion lleva el HASH del contenido del
ConfigMap, asi que cualquier edicion del codigo cambia el hash, cambia la
plantilla del pod, y ArgoCD dispara el rollout solo. Si alguien edita el sync sin
recalcular el hash, esto falla en CI y no en produccion.

Para recalcular tras editar el sync:
    python3 tests/test_configmap_revision_bump_contract.py --fix
"""
import hashlib
import pathlib
import re
import sys

import yaml


MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# ConfigMap cuyo contenido gobierna un Deployment que NO lo relee en caliente.
VIGILADOS = {
    "litellm-dgx-backend-sync": "litellm-dgx-backend-sync",
}
PREFIJO = "sync-"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _hash_configmap(nombre: str) -> str:
    for doc in _docs():
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == nombre:
            payload = "".join(
                f"{k}\n{v}" for k, v in sorted((doc.get("data") or {}).items())
            )
            return hashlib.sha256(payload.encode()).hexdigest()[:12]
    raise AssertionError(f"no encuentro el ConfigMap {nombre}")


def _anotacion(deployment: str) -> str | None:
    for doc in _docs():
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == deployment:
            anot = (
                doc["spec"]["template"]["metadata"].get("annotations") or {}
            )
            return anot.get("config.k8s.e-dani.com/revision")
    raise AssertionError(f"no encuentro el Deployment {deployment}")


def test_la_anotacion_lleva_el_hash_del_configmap_que_monta():
    for cm, deployment in VIGILADOS.items():
        esperado = PREFIJO + _hash_configmap(cm)
        actual = _anotacion(deployment)
        assert actual == esperado, (
            f"{deployment}: la anotacion dice {actual!r} y el contenido de {cm} "
            f"exige {esperado!r}.\n"
            "El pod NO relee el ConfigMap en caliente: sin bumpear esto, el "
            "cambio se aplica en git, ArgoCD dice Synced/Healthy y el proceso "
            "sigue con el codigo viejo hasta el proximo reinicio.\n"
            "Arreglalo con: python3 tests/test_configmap_revision_bump_contract.py --fix"
        )


def test_el_sync_sigue_sin_recargar_en_caliente():
    """Si algun dia el sync releyera su fichero, este contrato sobra.

    Se comprueba de forma explicita para que, si alguien anade recarga en
    caliente, este test le recuerde que puede retirar la anotacion en vez de
    dejarla como ceremonia perpetua.
    """
    for doc in _docs():
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-dgx-backend-sync":
            codigo = "".join((doc.get("data") or {}).values())
            break
    else:
        raise AssertionError("no encuentro el ConfigMap del sync")

    recarga = [m for m in ("importlib.reload", "inotify", "watchdog.observers") if m in codigo]
    assert not recarga, (
        f"el sync ya recarga en caliente ({recarga}): revisa si la anotacion "
        "config.k8s.e-dani.com/revision y este test siguen haciendo falta"
    )


def _fix() -> int:
    texto = MANIFEST.read_text()
    for cm, deployment in VIGILADOS.items():
        esperado = PREFIJO + _hash_configmap(cm)
        actual = _anotacion(deployment)
        if actual == esperado:
            print(f"  {deployment}: ya esta en {esperado}")
            continue
        patron = re.compile(
            r"(config\.k8s\.e-dani\.com/revision:\s*)" + re.escape(str(actual))
        )
        texto, n = patron.subn(r"\g<1>" + esperado, texto, count=1)
        assert n == 1, f"no pude sustituir la anotacion de {deployment}"
        print(f"  {deployment}: {actual} -> {esperado}")
    MANIFEST.write_text(texto)
    return 0


if __name__ == "__main__":
    if "--fix" in sys.argv:
        raise SystemExit(_fix())
    print(__doc__)
