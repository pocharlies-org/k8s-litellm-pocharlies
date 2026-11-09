"""SC-2917: el tope `max_parallel_requests` tiene la semantica DECLARADA abajo por imagen. Router REAL.

Medida contractual (la que DGX-874 hizo en vivo el 20-09 sobre v1.100.0, ahora dentro del repo):
N+1 llamadas concurrentes contra UNA sola deployment con `max_parallel_requests: N` (tomado del
manifiesto, no copiado) apuntando a un upstream falso que retiene cada llamada 1,5 s. Si la
(N+1)-esima ESPERA hueco y todas terminan, la semantica es ENCOLA; si alguien recibe 429
"already has ... requests in flight" en el acto, es RECHAZA.

TEST DE CARACTERIZACION TRANSITORIO (dictamen del arquitecto, SC-2917, opcion d): fija lo que
HAY, no lo que se desea — verde mientras la imagen pineada mantenga su semantica declarada, ROJO
si un bump cambia la semantica sin declararla o pinea una imagen sin medir. La semantica que los
topes 4/3 del residente presuponen es ENCOLA (asi estaba con v1.100.0); la imagen pineada hoy
declara RECHAZA como REGRESION de SC-2917 (fallo del clasificador del 23-10). El parche del fork
que restaure la espera cambia esta declaracion a ENCOLA en su propio PR.

Uso: tests/integration/run_semaphore_queue_contract.sh (necesita docker y la imagen).
"""
import asyncio
import json
import os
import pathlib
import time

import yaml
from litellm import Router
from litellm.exceptions import RateLimitError

MANIFEST = pathlib.Path(os.environ.get("REPO", ".")) / "k8s" / "manifest.yaml"
DEPLOYMENT_ID = "qwen38-flash-next-tp2"
HOLD_S = 1.5

# Semantica DECLARADA por imagen pineada: {tag: "ENCOLA"|"RECHAZA"}. Cada valor es un hecho
# MEDIDO con este mismo test contra esa imagen, no una sospecha. Imagen sin entrada, o cuya
# semantica medida cambie sin actualizar la entrada = CI rojo.
# SC-2917: RECHAZA es la REGRESION declarada (los topes 4/3 presuponen ENCOLA; DGX-874 midio
# ENCOLA con v1.100.0). El parche del fork (historia de SC-2917) la cambiara a ENCOLA.
SEMANTICA_DECLARADA = {
    "v1.105.1-codex-e1cfa8cbd5": "RECHAZA",  # medido 23-10-2026: acquire() levanta RateLimitError
}


def _tope_del_manifiesto() -> int:
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            config = yaml.safe_load(doc["data"]["config.yaml"])
            for m in config["model_list"]:
                if m["model_info"].get("id") == DEPLOYMENT_ID:
                    tope = m["litellm_params"]["max_parallel_requests"]
                    assert isinstance(tope, int) and tope > 0, tope
                    return tope
    raise SystemExit(f"no encuentro la deployment {DEPLOYMENT_ID} en el manifiesto")


async def _upstream_falso(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Un /v1/chat/completions que retiene HOLD_S y contesta un chat.completion valido."""
    header = await reader.readuntil(b"\r\n\r\n")
    length = int([h for h in header.decode().splitlines()
                  if h.lower().startswith("content-length:")][0].split(":")[1])
    await reader.readexactly(length)
    await asyncio.sleep(HOLD_S)
    cuerpo = json.dumps({
        "id": "chatcmpl-sem", "object": "chat.completion", "created": 0, "model": "sem-test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }).encode()
    cabecera = (f"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                f"content-length: {len(cuerpo)}\r\n\r\n").encode()
    writer.write(cabecera + cuerpo)
    await writer.drain()
    writer.close()


async def main() -> None:
    tope = _tope_del_manifiesto()
    server = await asyncio.start_server(_upstream_falso, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    router = Router(model_list=[{
        "model_name": "sem-test",
        "litellm_params": {"model": "openai/sem-test", "api_base": f"http://127.0.0.1:{port}/v1",
                          "api_key": "fake", "max_parallel_requests": tope},
    }], num_retries=0)

    async def una():
        t0 = time.monotonic()
        try:
            await router.acompletion(model="sem-test",
                                     messages=[{"role": "user", "content": "sem"}])
            return ("ok", time.monotonic() - t0)
        except RateLimitError as e:
            return ("429", str(e))
        except Exception as e:  # noqa: BLE001 - el contrato es contra cualquier fallo raro
            return ("error", f"{type(e).__name__}: {e}")

    async with server:
        resultados = await asyncio.gather(*(una() for _ in range(tope + 1)))

    rechazadas = [r for r in resultados if r[0] == "429"]
    raras = [r for r in resultados if r[0] == "error"]
    imagen = os.environ.get("SEM_TEST_IMAGE", "?")
    if rechazadas or raras:
        marca = "RECHAZA"
    else:
        # ENCOLA exige que la ultima en terminar haya esperado al menos el retardo del upstream:
        # sin espera, N+1 llamadas de 1,5 s acabarían en ~1,5 s en total.
        marca = "ENCOLA" if min(r[1] for r in resultados) >= HOLD_S * 0.8 else "PASAN-SIN-COLA"
    print(f"semaphore_queue_contract: imagen={imagen} tope={tope} llamadas={tope + 1} -> {marca}")
    for estado, detalle in rechazadas + raras:
        print(f"  {estado}: {detalle[:200]}")

    # "litellm:v1.105.1-codex-e1cfa8cbd5@sha256:95c..." -> "v1.105.1-codex-e1cfa8cbd5"
    tag = imagen.split("/")[-1].split("@")[0].split(":", 1)[-1]
    esperada = SEMANTICA_DECLARADA.get(tag)
    if marca == "PASAN-SIN-COLA":
        print("  INCONCLUYENTE: no hay ni rechazo ni espera medible; revisa el harness del test.")
    elif esperada is None:
        print(f"  SEMANTICA NO DECLARADA para {tag}: imagen pineada sin medir (o sin su entrada).")
    elif marca != esperada:
        nota = " (REGRESION SC-2917: los topes presuponen ENCOLA)" if esperada == "RECHAZA" else ""
        print(f"  CAMBIO SIN DECLARAR: {tag} declarada {esperada}, medida {marca}{nota}.")
    else:
        print(f"  coincide con lo declarado para {tag}: {esperada}")
        return
    raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
