"""`dev/session_router.py` es copia legible del `session_router.py` embebido en el
ConfigMap `litellm-config`: el manifiesto manda y lo que corre es el embed.

Seguimiento del veredicto del architect en DGX-620 (el seam `draw_account` se
toco en los dos sitios a mano). Si alguien edita uno y olvida el otro, este test
falla con el primer trozo de diff y no hay que descubrirlo en produccion.
"""
import difflib
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
DEV = ROOT / "dev" / "session_router.py"


def _embed() -> str:
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return doc["data"]["session_router.py"]
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def test_la_copia_de_dev_es_identica_al_embed_del_manifiesto():
    embed, dev = _embed().rstrip(), DEV.read_text().rstrip()
    if embed == dev:
        return
    diff = difflib.unified_diff(
        embed.splitlines(), dev.splitlines(),
        fromfile="k8s/manifest.yaml (litellm-config: session_router.py)",
        tofile="dev/session_router.py", lineterm="", n=2,
    )
    primer = "\n".join(list(diff)[:40])
    raise AssertionError(
        "dev/session_router.py y el embed del manifiesto divergen. El manifiesto manda: "
        "copia el embed a dev/ (o el cambio de dev/ al manifiesto) y recalcula el hash con "
        "`python3 tests/test_configmap_revision_bump_contract.py --fix`.\n" + primer
    )
