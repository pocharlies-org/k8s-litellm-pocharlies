"""El env del contenedor `litellm` lleva MALLOC_ARENA_MAX=2 (SC-352, 09-09).

POR QUE EXISTE. El 04-09, con el trafico parado 4 h, el working set del proxy
no bajo de 6,5 GiB — memoria adquirida y no devuelta al sistema; tras el
reinicio, prompts 2,4x mayores quedaron contenidos. Crecimiento que depende del
camino recorrido y no de la carga instantanea es la firma de la fragmentacion
de arenas del asignador de glibc. Es HIPOTESIS, no causa raiz (la linea de
diagnostica esta cerrada por dictamen del VP del 07-09: el sintoma no es
reproducible, banda plana 1,10-1,61 GiB en 18 h). La historia SC-352 se juzga
por SIN REGRESION, no por mejora.

Que el tope de 2 arenas se mantenga en el Deployment es lo unico que este test
garantiza: si alguien lo borra "de paso" al tocar el env, el manifiesto vuelve
silenciosamente a las arenas por defecto (8 x nucleos) y el PR de SC-352 deja
de estar desplegado sin que nada lo avise.
"""
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _litellm_container():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "litellm":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "litellm":
                    return c
    raise AssertionError("no encuentro el contenedor litellm del Deployment litellm")


def test_litellm_container_caps_glibc_arenas():
    env = {e["name"]: e.get("value") for e in _litellm_container().get("env", [])}
    assert env.get("MALLOC_ARENA_MAX") == "2", (
        "MALLOC_ARENA_MAX=2 ausente o alterado en el env de litellm: la "
        "mitigacion SC-352 ya no esta desplegada")
