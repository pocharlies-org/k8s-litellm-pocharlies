"""Contrato del master/replica de codex-bridge: refrescar exige el Lease.

El refresh consume el refresh_token y OpenAI lo ROTA: dos refrescos concurrentes
de la misma credencial se matan la sesion (y rehacerla es un `codex login`
manual). Con dos replicas por bridge, la unica defensa es que el refresh de
emergencia sea EXCLUSIVO, y la exclusion la da un Lease de Kubernetes cuyo CAS
hace cumplir el apiserver.

Lo que este fichero fija, y por que:

* El perdedor del Lease NO llama a OpenAI. Ni una vez. Un "refresco y ya se
  arreglara" del perdedor es exactamente la muerte de sesion que esto previene.
* Antes de refrescar se adopta el Secret VIA API, no solo del volumen: el
  kubelet propaga con ~1 min de retraso y esa ventana es la carrera.
* Un Lease expirado (holder muerto a mitad de refresh) se puede robar; uno vivo
  de otro pod, jamas.
* Los dos Deployments corren 2 replicas con antiafinidad dura por hostname, y
  cada cuenta tiene su Lease y su RBAC (create sin resourceNames + get/update
  restringido: `create` no admite nombres porque el objeto aun no existe).

Se extraen las funciones REALES de bridge.py del ConfigMap, igual que el resto
de contratos de este repo.
"""
import ast
import base64
import json
import logging
import types
from pathlib import Path

import pytest
import yaml

BRIDGE_YAML = Path(__file__).resolve().parents[1] / "k8s" / "codex-bridge.yaml"

WANT_FN = {"acquire_refresh_lease", "release_refresh_lease", "read_secret_auth",
           "_lease_now", "_lease_parse", "_jwt_claim", "_jwt_exp"}


def _docs():
    return [d for d in yaml.safe_load_all(BRIDGE_YAML.read_text()) if d]


@pytest.fixture(scope="module")
def bridge():
    cm = next(d for d in _docs()
              if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "codex-bridge-code")
    mod = types.ModuleType("bridgepure")
    # Cargar el modulo entero es seguro: main() solo corre con __name__ ==
    # "__main__". Antes se extraian todos los ast.Assign junto a unas funciones;
    # eso convertia cualquier nuevo global con dependencias (como ADMISSION_GATE)
    # en un NameError aunque bridge.py fuera perfectamente valido.
    exec(compile(cm["data"]["bridge.py"], "<bridge>", "exec"), mod.__dict__)
    found = {name for name in WANT_FN if callable(getattr(mod, name, None))}
    assert WANT_FN <= found, f"bridge.py ya no define: {sorted(WANT_FN - found)}"
    mod.log = lambda msg: logging.getLogger("test.bridge").info(msg)
    return mod


class FakeK8s:
    """Apiserver de mentira con la semantica que importa: el CAS del PUT."""

    def __init__(self, lease=None):
        self.lease = lease
        self.calls = []
        self.conflict_on_put = False

    def __call__(self, method, path, body=None, content_type=None):
        self.calls.append((method, path))
        if "/leases" in path:
            if method == "GET":
                return (200, self.lease) if self.lease else (404, None)
            if method == "POST":
                if self.lease:
                    return 409, None
                self.lease = json.loads(body)
                return 201, self.lease
            if method == "PUT":
                if self.conflict_on_put:
                    return 409, None
                self.lease = json.loads(body)
                return 200, self.lease
        raise AssertionError(f"llamada inesperada: {method} {path}")


def _lease(holder, bridge, age_seconds=0):
    stamp = bridge.time.strftime(
        "%Y-%m-%dT%H:%M:%S.000000Z",
        bridge.time.gmtime(bridge.time.time() - age_seconds))
    return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
            "metadata": {"name": "codex-bridge-refresh", "resourceVersion": "7"},
            "spec": {"holderIdentity": holder, "leaseDurationSeconds": 120,
                     "renewTime": stamp}}


def test_sin_lease_previo_lo_crea_y_es_holder(bridge):
    fake = FakeK8s()
    bridge._k8s = fake
    bridge.POD_NAME = "bridge-a"
    assert bridge.acquire_refresh_lease() is True
    assert fake.lease["spec"]["holderIdentity"] == "bridge-a"


def test_el_perdedor_no_es_holder_con_un_lease_vivo_de_otro(bridge):
    fake = FakeK8s(_lease("bridge-a", bridge, age_seconds=5))
    bridge._k8s = fake
    bridge.POD_NAME = "bridge-b"
    assert bridge.acquire_refresh_lease() is False
    # Y NO intento escribir: perder es leer y marcharse.
    assert all(m == "GET" for m, _ in fake.calls)


def test_un_lease_expirado_se_roba(bridge):
    """El holder murio a mitad de refresh: pasado el TTL, otro puede entrar."""
    fake = FakeK8s(_lease("bridge-a", bridge, age_seconds=500))
    bridge._k8s = fake
    bridge.POD_NAME = "bridge-b"
    assert bridge.acquire_refresh_lease() is True
    assert fake.lease["spec"]["holderIdentity"] == "bridge-b"


def test_perder_el_CAS_del_apiserver_es_perder(bridge):
    """Dos pods ven el lease expirado a la vez: el 409 del PUT decide."""
    fake = FakeK8s(_lease("bridge-a", bridge, age_seconds=500))
    fake.conflict_on_put = True
    bridge._k8s = fake
    bridge.POD_NAME = "bridge-b"
    assert bridge.acquire_refresh_lease() is False


def test_release_solo_suelta_lo_propio(bridge):
    fake = FakeK8s(_lease("bridge-a", bridge, age_seconds=5))
    bridge._k8s = fake
    bridge.POD_NAME = "bridge-b"
    bridge.release_refresh_lease()
    assert fake.lease["spec"]["holderIdentity"] == "bridge-a"  # intacto
    bridge.POD_NAME = "bridge-a"
    bridge.release_refresh_lease()
    assert fake.lease["spec"]["holderIdentity"] == ""


def test_el_refresh_de_emergencia_esta_gateado_por_el_lease_en_el_codigo():
    """Estructural: en refresh_if_needed, ningun POST a OpenAI puede ejecutarse
    sin haber pasado antes por acquire_refresh_lease. Se comprueba en el fuente
    para que un refactor no pueda quitar el gate sin que esto caiga."""
    cm = next(d for d in _docs()
              if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "codex-bridge-code")
    src = cm["data"]["bridge.py"]
    fn = src.split("def refresh_if_needed():")[1].split("\n    # ---")[0]
    gate = fn.index("acquire_refresh_lease()")
    llamada_openai = fn.index("TOKEN_URL")
    assert gate < llamada_openai, "el POST a OpenAI ocurre antes del gate del Lease"
    # y el perdedor retorna sin refrescar
    tramo_perdedor = fn[fn.index("acquire_refresh_lease()"):fn.index("TOKEN_URL")]
    assert "return" in tramo_perdedor
    # rotate.py, mismo contrato
    rot = cm["data"]["rotate.py"]
    cuerpo = rot.split("def main():")[1]
    assert cuerpo.index("acquire_lease()") < cuerpo.index("TOKEN_URL")


def test_deployments_replicas_antiafinidad_y_leases_por_cuenta():
    docs = _docs()
    esperado = {
        "codex-bridge": ("codex-bridge-refresh", "codex-bridge-auth"),
        "codex-bridge-edani": ("codex-bridge-edani-refresh", "codex-bridge-edani-auth"),
    }
    for nombre, (lease, secreto) in esperado.items():
        dep = next(d for d in docs if d.get("kind") == "Deployment"
                   and d["metadata"]["name"] == nombre)
        assert dep["spec"]["replicas"] == 2, nombre
        tpl = dep["spec"]["template"]["spec"]
        anti = tpl["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        assert anti[0]["topologyKey"] == "kubernetes.io/hostname", nombre
        envs = {e["name"]: e.get("value") for c in tpl["containers"] for e in c.get("env", [])}
        assert envs.get("LEASE_NAME") == lease, nombre
        assert "POD_NAME" in envs, nombre
        # RBAC: get/update SOLO sobre su lease; create sin nombres (no existe aun)
        role = next(d for d in docs if d.get("kind") == "Role"
                    and d["metadata"]["name"] == nombre)
        reglas = role["rules"]
        named = [r for r in reglas if r.get("resources") == ["leases"] and r.get("resourceNames")]
        assert named and named[0]["resourceNames"] == [lease], nombre
        assert set(named[0]["verbs"]) == {"get", "update"}, nombre
        creates = [r for r in reglas if r.get("resources") == ["leases"] and not r.get("resourceNames")]
        assert creates and creates[0]["verbs"] == ["create"], nombre


def test_los_cronjobs_toman_el_mismo_lease_que_su_bridge():
    docs = _docs()
    esperado = {"codex-token-rotate": "codex-bridge-refresh",
                "codex-token-rotate-edani": "codex-bridge-edani-refresh"}
    for nombre, lease in esperado.items():
        cron = next(d for d in docs if d.get("kind") == "CronJob"
                    and d["metadata"]["name"] == nombre)
        tpl = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        envs = {e["name"]: e.get("value") for c in tpl["containers"] for e in c.get("env", [])}
        assert envs.get("LEASE_NAME") == lease, nombre
