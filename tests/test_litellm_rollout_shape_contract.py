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


def test_surge_without_spread(deploy):
    """Surge 1 / maxUnavailable 0, y SIN topologySpreadConstraints.

    El surge sigue siendo lo que desserializa el rollout (sin el, cada replica
    espera los 720s de drenaje de la anterior). El reparto por hostname se quito
    el 29-09: con `sauvage` elegible como failover, un spread (aunque fuera
    ScheduleAnyway) mandaria la segunda replica a OVH en cada rollout y el
    descheduler la devolveria -- ida y vuelta. Sin spread no hay deadlock posible:
    el pod de surge cabe en cualquier nodo elegible.
    """
    rolling = deploy["spec"]["strategy"]["rollingUpdate"]
    assert rolling["maxSurge"] == 1, (
        "sin surge el rollout serializa: cada replica espera los 720s de drenaje "
        "de la anterior")
    assert rolling["maxUnavailable"] == 0, (
        "con maxUnavailable > 0 se pierde capacidad durante el rollout, y con solo "
        "2 replicas eso es la mitad del proxy de TODO el trafico LLM")
    assert not deploy["spec"]["template"]["spec"].get("topologySpreadConstraints"), (
        "un spread por hostname con sauvage elegible reparte una replica a OVH en "
        "cada rollout y el descheduler la devuelve: ida y vuelta")


def test_replicas_keep_HA_without_over_provisioning(deploy):
    """Por que 2 y no 1: es el proxy de TODO el trafico LLM, y 1 replica es punto
    unico de fallo con cola. Por que no mas de 4: decision del owner (19-08, "4 era
    sobre-arquitectura") con 5-6m de CPU real por pod y concurrencia media 6,68.
    """
    replicas = deploy["spec"]["replicas"]
    assert 2 <= replicas <= 4, (
        f"{replicas} replicas: por debajo de 2 no hay HA frente a la caida de un "
        f"pod; por encima de 4 es sobre-aprovisionar un proxy async I/O-bound")


def test_the_PDB_cannot_block_a_node_drain(deploy):
    """El riesgo real de bajar replicas.

    Con 2 replicas un `minAvailable: 2` da allowedDisruptions=0 y CUELGA cualquier
    drenaje de nodo -- rutina en los ks5. Se expresa como `maxUnavailable` para que
    siga siendo correcto si las replicas vuelven a cambiar.

    29-09-2026: con `sauvage` como failover, drenar `ubuntu` ya no se cuelga: el
    pod desalojado tiene donde ir, y el descheduler lo devuelve al descordonar.
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


# --------------------------------------------------------------------------
# SC-294 (08-09): las sondas que faltaban el 05-09.
#
# El 05-09 a las 00:04Z litellm dejo de responder a /health/liveliness y NO se
# recupero solo: no habia livenessProbe y hubo que sacarlo a mano con un commit
# que bumpeaba el hash del ConfigMap. Estas aserciones codifican la forma que
# cierra ese hueco (liveness + startup sobre /health/liveliness, umbrales
# conservadores) y la que impide re-abrirlo por despiste (readiness NO se mueve
# a /health/readiness pese a que la doc de LiteLLM la sugiere: ver el
# comentario del manifiesto y el incidente del 20-07).
# --------------------------------------------------------------------------

# Arranque MEDIDO en el rollout del 07-09 (kubectl logs --timestamps del RS
# litellm-6999f45cbc): del arranque del contenedor al primer 200 de
# /health/liveliness, 34s (8tgbv) y 41s (7gzt4). El presupuesto de la
# startupProbe tiene que cubrir al menos 3x el peor caso medido.
MEASURED_STARTUP_SECONDS = 41


def _container(deploy, name):
    for c in deploy["spec"]["template"]["spec"]["containers"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"no encuentro el contenedor {name}")


def test_litellm_has_liveness_and_startup_probes(deploy):
    """El hueco del 05-09: sin livenessProbe un event loop colgado no se cura solo."""
    c = _container(deploy, "litellm")
    assert "livenessProbe" in c, (
        "sin livenessProbe un litellm con el event loop colgado se queda colgado "
        "para siempre (medido 05-09: hubo que sacarlo a mano)")
    assert "startupProbe" in c, (
        "sin startupProbe, endurecer liveness para cubrir el arranque significa "
        "deteccion lenta de deadlocks, y ablandarla significa reinicios en un "
        "arranque lento; la startup es lo que desacopla las dos cosas")


def test_probes_point_at_liveliness_never_health(deploy):
    """Las tres sondas van a /health/liveliness:4000.

    /health NUNCA va en una sonda: la doc de LiteLLM dice que "By default
    /health probes every model on each call" y hace una peticion real a cada
    modelo -- sondearlo cada 15s gastaria tokens y tumbria upstreams.
    /health/readiness NUNCA en readiness: hace round-trip a Postgres y saca el
    pod del Service con la inferencia sana (incidente 2026-07-20, 9h; el proxy
    sirve a proposito con la DB caida via allow_requests_on_db_unavailable).
    """
    c = _container(deploy, "litellm")
    for probe in ("livenessProbe", "startupProbe", "readinessProbe"):
        hg = c[probe]["httpGet"]
        assert hg["path"] == "/health/liveliness", (
            f"{probe} apunta a {hg['path']}: solo /health/liveliness es 200 "
            "mientras el event loop responde sin mirar dependencias")
        assert hg["port"] == 4000


def test_liveness_thresholds_stay_conservative(deploy):
    """Deteccion de cuelgue sin tormenta de reinicios.

    La doc de k8s advierte: una liveness mal calibrada reinicia bajo carga y
    amplifica el incidente. Los limites de abajo son los del diseno SC-294:
    deteccion en ~60-75s (4 fallos x 15s), y timeout >= 5s porque el 05-09 el
    endpoint no es que fuera lento, es que no contestaba.
    """
    p = _container(deploy, "litellm")["livenessProbe"]
    assert 10 <= p["periodSeconds"] <= 15, (
        f"periodSeconds {p['periodSeconds']}: por debajo de 10 sondea demasiado "
        "y por encima de 15 el cuelgue tarda demasiado en detectarse")
    assert 3 <= p["failureThreshold"] <= 5, (
        f"failureThreshold {p['failureThreshold']}: con menos, un pico de "
        "latencia reinicia el proxy de TODO el trafico LLM")
    assert p["timeoutSeconds"] >= 5, (
        "timeout < 5s convierte un arranque lento en reinicio")


def test_startup_budget_covers_measured_boot_three_times(deploy):
    """El presupuesto de arranque es medida, no supersticion.

    periodSeconds * failureThreshold >= 3x el peor arranque medido (41s). El
    05-09 el problema era que NO habia sonda; el problema del diseno contrario
    (liveness agresiva sin startup) seria un bucle de reinicios en cada rollout.
    """
    p = _container(deploy, "litellm")["startupProbe"]
    budget = p["periodSeconds"] * p["failureThreshold"]
    assert budget >= 3 * MEASURED_STARTUP_SECONDS, (
        f"presupuesto de arranque {budget}s < 3x el peor arranque medido "
        f"({MEASURED_STARTUP_SECONDS}s): un rollout con el nodo cargado entraria "
        "en bucle de reinicios antes de terminar de arrancar")


# --------------------------------------------------------------------------
# 29-09-2026: el anclaje a `ubuntu` (SC-404) pasa a PREFERENCIA con failover a
# `sauvage`, tras el corte de luz del x86 que dejo sin proxy a todo el trafico
# LLM con los Sparks sirviendo. Detalle en doc/node-affinity-ubuntu.md.
# --------------------------------------------------------------------------


def _node_affinity(deploy):
    return deploy["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]


def test_prefers_x86_and_fails_over_to_sauvage_only(deploy):
    """Elegibles SOLO `ubuntu` y `sauvage`; en reposo, `ubuntu`.

    Ni los Sparks (dedicated=llm, memoria unificada, SystemOOM del 19-08) ni el
    plano de control ks5. Y sin tolerar `dedicated`: eso es decision del owner
    del pool de GPU, no de este manifiesto.
    """
    na = _node_affinity(deploy)
    req = na["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert req == [{"matchExpressions": [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["ubuntu", "sauvage"]}]}]
    pref = na["preferredDuringSchedulingIgnoredDuringExecution"]
    assert pref == [{"weight": 100, "preference": {"matchExpressions": [
        {"key": "kubernetes.io/hostname", "operator": "In", "values": ["ubuntu"]}]}}]
    tol = deploy["spec"]["template"]["spec"]["tolerations"]
    assert not [t for t in tol if t.get("key") == "dedicated"], (
        "tolerar dedicated=llm:NoSchedule mete al router en el pool dedicado de GPU")
    assert {"key": "role", "operator": "Equal", "value": "edge",
            "effect": "NoSchedule"} in tol, "sin tolerar role=edge no cabe en sauvage"


def test_failover_is_fast_and_comes_back(deploy):
    """30 s de toleracion a unreachable/not-ready (defecto 300) y la etiqueta
    de opt-in del descheduler que lo devuelve al x86 cuando vuelve."""
    tol = {t["key"]: t for t in deploy["spec"]["template"]["spec"]["tolerations"]}
    for key in ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready"):
        assert tol[key]["effect"] == "NoExecute"
        assert tol[key]["tolerationSeconds"] <= 60, f"{key}: failover de minutos"
    labels = deploy["spec"]["template"]["metadata"]["labels"]
    assert labels.get("e-dani.com/vuelve-a-x86") == "true", (
        "sin la etiqueta el descheduler no lo devuelve al x86 y se queda en OVH")
