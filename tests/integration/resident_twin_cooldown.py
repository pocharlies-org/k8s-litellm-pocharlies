"""19-10-2026: la familia residente NUNCA aparca en el Router. Router REAL de la imagen pineada.

El model_list y el router_settings salen del manifiesto (no copiados): los cuatro gemelos `-tpN`,
`correo-clasificador` y los perfiles `tooling`. Cada fallo pasa por los dos caminos del Router que
pueden aparcar una deployment, con los mismos `cooldown_time` que usa en produccion:
  1. el fallo de la llamada (`Router.deployment_callback_on_failure`): `cooldown_time` de la deployment;
  2. el pre-call (`async_routing_strategy_pre_call_checks`): un `RateLimitError` de un callback
     (semaforo lleno) aparca con el `cooldown_time` del ROUTER.
Ni timeouts, ni 500, ni 429 de pre-call deben aparcar a nadie. Un motor caido sale por `live_backends`.
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
FAMILIA = ("qwen38-flash-next-tp2", "qwen38-flash-next-tp1",
           "qwen38-flash-next-uncensored-tp2", "qwen38-flash-next-uncensored-tp1",
           "correo-clasificador")
VECES = 60


def _config():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise SystemExit("no encuentro el ConfigMap litellm-config")


def _router():
    config = _config()
    rs = config["router_settings"]
    model_list = [m for m in config["model_list"] if m["model_info"].get("id") in FAMILIA]
    assert len(model_list) == len(FAMILIA), [m["model_info"].get("id") for m in model_list]
    return Router(model_list=model_list, allowed_fails=rs["allowed_fails"], cooldown_time=rs["cooldown_time"])


def _aparca(router, dep_id, exc, status, veces, time_to_cooldown):
    """True si alguna de las `veces` aparca la deployment con ese `time_to_cooldown`."""
    dep = router.get_model_info(id=dep_id)
    aparcada = False
    for _ in range(veces):
        increment_deployment_failures_for_current_minute(litellm_router_instance=router, deployment_id=dep_id)
        aparcada |= bool(_set_cooldown_deployments(
            litellm_router_instance=router, original_exception=exc, exception_status=status,
            deployment=dep_id, time_to_cooldown=time_to_cooldown,
            requested_model_group=dep["model_name"]))
    return aparcada


async def main():
    # async: al aparcar, el Router lanza su callback de cooldown con create_task
    timeout = litellm.Timeout(message="timeout value=240.0", model="qwen38-flash-next", llm_provider="openai")
    caido = litellm.InternalServerError(message="Connection error.", llm_provider="openai", model="qwen38-flash-next")
    saturado = litellm.RateLimitError(message="max_parallel_requests", llm_provider="openai", model="qwen38-flash-next")
    for dep_id in FAMILIA:
        router = _router()
        propio = _first_present(router.get_model_info(id=dep_id).get("model_info"), key="cooldown_time")
        assert propio == 0, f"{dep_id}: cooldown_time {propio}, debe ser 0"
        assert not _aparca(router, dep_id, timeout, 408, VECES, propio), f"{dep_id}: timeouts la aparcaron"
        assert not _aparca(router, dep_id, caido, 500, VECES, propio), f"{dep_id}: 500 la aparcaron"
        assert not _aparca(router, dep_id, saturado, 429, VECES, router.cooldown_time), (
            f"{dep_id}: el pre-call con el cooldown del router la aparco"
        )
    print(f"19-10-2026: {len(FAMILIA)} deployments del residente, {VECES} timeouts, 500 y 429 de pre-call: ninguna aparcada")


if __name__ == "__main__":
    asyncio.run(main())
