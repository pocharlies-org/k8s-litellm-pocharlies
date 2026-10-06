"""plan-gateway (DGX-621) dentro de la imagen PINEADA del proxy.

Los unitarios corren en el Python del runner; el gateway corre en el de la imagen.
Aqui se extraen del manifiesto el `plan_gateway.py` y el `session_router.py` que
monta el Deployment, se importan con las dependencias REALES de la imagen
(httpx, uvicorn, fastapi, litellm) y se le pide al ASGI un 404 y un 401 sin red.
"""
import asyncio
import os
import pathlib
import sys
import tempfile

import httpx
import yaml

REPO = pathlib.Path(os.environ.get("REPO", "/repo"))
docs = [d for d in yaml.safe_load_all((REPO / "k8s" / "manifest.yaml").read_text()) if d]
cm = {d["metadata"]["name"]: d["data"] for d in docs if d.get("kind") == "ConfigMap"}

work = pathlib.Path(tempfile.mkdtemp())
(work / "plan_gateway.py").write_text(cm["plan-gateway-config"]["plan_gateway.py"])
(work / "session_router.py").write_text(cm["litellm-config"]["session_router.py"])
sys.path.insert(0, str(work))
os.environ["PLAN_GATEWAY_TOKEN_STUDIO"] = "tok"
os.environ["DASHSCOPE_API_KEY"] = "sk"

import plan_gateway  # noqa: E402


async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=plan_gateway.app), base_url="http://gw") as c:
        assert (await c.get("/nada")).status_code == 404
        r = await c.post("/api/v1/services/aigc/multimodal-generation/generation", content=b"{}")
        assert r.status_code == 401, r.status_code


asyncio.run(main())
print("plan_gateway: import y ASGI ok en", sys.version.split()[0])
