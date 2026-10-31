"""DGX-1103: un timeout no aparca a un gemelo del residente; un 500 sí. Router REAL de la imagen pineada.

El model_list y el router_settings salen del manifiesto (no copiados): los cuatro gemelos `-tpN` y
`correo-clasificador`. Cada fallo pasa por lo mismo que `Router.deployment_callback_on_failure`: el
contador por minuto y `_set_cooldown_deployments` con el `cooldown_time` de la deployment.
Uso: tests/integration/run_alibaba_account_affinity.sh (necesita docker y la imagen).
"""
import asyncio
import os
import pathlib

import litellm
import yaml
from litellm import Router
from litellm.router_utils.cooldown_handlers import _first_present, _set_cooldown_deployments
from litellm.router_utils.router_callbacks.track_deployment_metrics import (
    increment_deployment_failures_for_current_minute,
)

MANIFEST = pathlib.Path(os.environ.get("REPO", ".")) / "k8s" / "manifest.yaml"
GEMELOS = ("qwen38-flash-next-tp2", "qwen38-flash-next-tp1",
           "qwen38-flash-next-uncensored-tp2", "qwen38-flash-next-uncensored-tp1")


def _config():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise SystemExit("no encuentro el ConfigMap litellm-config")


def _router():
    config = _config()
    rs = config["router_settings"]
    model_list = [m for m in config["model_list"]
                  if m["model_info"].get("id") in GEMELOS + ("correo-clasificador",)]
    assert len(model_list) == 5, [m["model_info"].get("id") for m in model_list]
    return Router(model_list=model_list, allowed_fails=rs["allowed_fails"], cooldown_time=rs["cooldown_time"])


def _falla(router, dep_id, exc, status, veces):
    """True si alguna de las `veces` aparca la deployment."""
    dep = router.get_model_info(id=dep_id)
    propio = _first_present(dep.get("model_info"), dep.get("litellm_params"), key="cooldown_time")
    aparcada = False
    for _ in range(veces):
        increment_deployment_failures_for_current_minute(litellm_router_instance=router, deployment_id=dep_id)
        aparcada |= bool(_set_cooldown_deployments(
            litellm_router_instance=router, original_exception=exc, exception_status=status,
            deployment=dep_id, time_to_cooldown=propio if propio is not None else router.cooldown_time,
            requested_model_group=dep["model_name"]))
    return aparcada


async def main():
    # async: al aparcar, el Router lanza su callback de cooldown con create_task
    timeout = litellm.Timeout(message="timeout value=240.0", model="qwen38-flash-next", llm_provider="openai")
    caido = litellm.InternalServerError(message="Connection error.", llm_provider="openai", model="qwen38-flash-next")
    for dep_id in GEMELOS:
        router = _router()
        assert not _falla(router, dep_id, timeout, 408, 50), f"{dep_id}: 50 timeouts lo aparcaron"
        assert _falla(router, dep_id, caido, 500, 4), f"{dep_id}: 4 errores 500 no lo aparcaron"
    router = _router()
    assert router.get_model_info(id="correo-clasificador")["model_info"].get("allowed_fails_policy") is None
    assert not _falla(router, "correo-clasificador", caido, 500, 12), "correo-clasificador: 12 errores 500 lo aparcaron"
    print("DGX-1103: timeouts no aparcan a ningun gemelo; 500 si; correo-clasificador sin aparcar")


if __name__ == "__main__":
    asyncio.run(main())
