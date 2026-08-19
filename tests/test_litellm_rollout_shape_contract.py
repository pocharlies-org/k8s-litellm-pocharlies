"""La forma del rollout de `litellm`, que costo 50 minutos aprender.

POR QUE EXISTE. Hasta el 19-08 este Deployment tardaba ~50 min en rodar: cuatro
reemplazos EN SERIE de 720s cada uno. La causa no era falta de capacidad, era la
combinacion `maxSurge: 0` + `maxSkew: 1` + `whenUnsatisfiable: DoNotSchedule` sobre
exactamente los cuatro nodos del nodeAffinity. Al bajar una replica el reparto
quedaba 1,1,1,0 y el pod nuevo SOLO cabia en el dominio con cuenta 0 -- el nodo que
se acababa de vaciar y que no suelta su CPU hasta que el viejo termina de drenar.

Arreglado en b1a15b1: 2 replicas, `maxSurge: 1 / maxUnavailable: 0` y
`ScheduleAnyway`. Medido: **134s** de punta a punta, `READY` nunca bajo de 2, cero
`FailedScheduling`, y los pods viejos drenaron sus 720s EN PARALELO y de fondo.

El intento anterior (14-08) fue `maxSurge: 1` con `DoNotSchedule` y dejo el rollout
MUERTO. Por eso los dos ajustes van juntos y este test los comprueba juntos: quien
devuelva `DoNotSchedule` sin quitar el surge revive el candado, y quien quite el
surge dejando `ScheduleAnyway` revive los 720s en serie. Ninguna de las dos cosas
da un error visible -- da un rollout lento o colgado, que es lo que hace falta un
test para verlo.
"""
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _named(kind, name):
    for doc in _docs():
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"no encuentro {kind}/{name}")


@pytest.fixture(scope="module")
def deploy():
    return _named("Deployment", "litellm")


def test_surge_and_ScheduleAnyway_travel_together(deploy):
    """Los dos ajustes son un solo arreglo. Separarlos revive un fallo distinto.

    - surge sin ScheduleAnyway  -> el pod extra no cabe en el unico dominio libre
      y el rollout se cuelga (medido 14-08).
    - ScheduleAnyway sin surge  -> vuelve el reemplazo en serie de 720s por replica.
    """
    rolling = deploy["spec"]["strategy"]["rollingUpdate"]
    spread = deploy["spec"]["template"]["spec"]["topologySpreadConstraints"]

    assert rolling["maxSurge"] == 1, (
        "sin surge el rollout serializa: cada replica espera los 720s de drenaje "
        "de la anterior")
    assert rolling["maxUnavailable"] == 0, (
        "con maxUnavailable > 0 se pierde capacidad durante el rollout, y con solo "
        "2 replicas eso es la mitad del proxy de TODO el trafico LLM")
    assert [c["whenUnsatisfiable"] for c in spread] == ["ScheduleAnyway"], (
        "DoNotSchedule + surge es el deadlock del 14-08: el pod extra solo cabe en "
        "el dominio con cuenta 0, que es justo el nodo que aun no ha soltado la CPU")


def test_the_skew_arithmetic_still_holds_for_the_declared_replicas(deploy):
    """`maxSkew: 1` tiene que seguir siendo satisfacible en reposo Y en rollout.

    Con R replicas sobre D dominios el reparto mas plano posible da skew
    ceil(R/D) - floor(R/D), que solo es <= 1 mientras R no pase de 2*D. En rollout
    hay R+maxSurge pods. Si alguien sube replicas sin mirar esto, el reparto deja de
    ser satisfacible y `ScheduleAnyway` lo degrada en silencio a "donde quepa" --
    los dos pods pueden acabar en el mismo nodo y se pierde la HA sin un aviso.
    """
    spec = deploy["spec"]
    sp = spec["template"]["spec"]
    replicas = spec["replicas"]
    surge = spec["strategy"]["rollingUpdate"]["maxSurge"]
    domains = len(
        sp["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        ["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
    )
    max_skew = sp["topologySpreadConstraints"][0]["maxSkew"]

    for label, pods in (("en reposo", replicas), ("en rollout", replicas + surge)):
        skew = -(-pods // domains) - (pods // domains)  # ceil - floor
        assert skew <= max_skew, (
            f"{label}: {pods} pods sobre {domains} dominios da skew {skew} > "
            f"{max_skew}; el reparto deja de ser satisfacible")


def test_replicas_keep_HA_without_over_provisioning(deploy):
    """2 es la decision del owner (19-08): 4 era sobre-arquitectura.

    Medido con las 4 arriba: 5-6m de CPU real por pod contra 300m reservados, y
    concurrencia solapada media 6,68. 1 replica seria punto unico de fallo con cola
    para el proxy de todo el trafico LLM; 3 o mas vuelven a pelearse por el hueco de
    CPU del pool ks5 durante el surge.
    """
    assert deploy["spec"]["replicas"] == 2


def test_the_PDB_cannot_block_a_node_drain(deploy):
    """El riesgo real de bajar replicas.

    Con 2 replicas un `minAvailable: 2` da allowedDisruptions=0 y CUELGA cualquier
    drenaje de nodo -- rutina en los ks5. Se expresa como `maxUnavailable` para que
    siga siendo correcto si las replicas vuelven a cambiar.
    """
    spec = _named("PodDisruptionBudget", "litellm")["spec"]
    assert "minAvailable" not in spec, (
        "minAvailable acoplado al numero de replicas bloquea drenajes al bajarlas")
    assert spec["maxUnavailable"] == 1


def test_the_drain_grace_is_untouched(deploy):
    """Los 720s NO son el problema y no hay que recortarlos.

    Con el reemplazo en paralelo los drenajes se solapan y dejan de estar en el
    camino critico: el rollout medido fue de 134s con los mismos 720s de gracia.
    Recortarlos cortaria streams en vuelo para arreglar algo que ya no duele.
    """
    sp = deploy["spec"]["template"]["spec"]
    assert sp["terminationGracePeriodSeconds"] == 720
    # Y el deadline tiene que dejar sitio a un arranque lento sin ser el doble de
    # un rollout que ahora dura poco mas de dos minutos.
    assert deploy["spec"]["progressDeadlineSeconds"] == 600
