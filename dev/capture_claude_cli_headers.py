"""Captura la cabecera REAL del Claude CLI instalado, para comparar antes de subir version.

Es el paso previo obligado del contrato dgx.litellm.claude-official-headers: antes de
cambiar CLAUDE_CLI_VERSION en dev/claude_fingerprints.py se corre esto con el CLI nuevo
y se mira lo que manda de verdad (user-agent, familia x-stainless-*, anthropic-beta).
Sale 1 si el CLI no llega al sink o si faltan claves que el contrato fija.

Dos modos, dos tramos del cable:
  (por defecto)  lo que manda el CLI instalado → lo que recibe litellm por delante.
  --cable        lo que litellm manda de verdad al egress → lo que veria Anthropic. El
                 sink hace de api.anthropic.com y corre el Router de litellm con el
                 bloque `headers:` del manifiesto tal cual. Necesita litellm instalado:
                 se corre dentro del pod del proxy.

Uso:  python3 -I dev/capture_claude_cli_headers.py [--claude <ruta>] [--cable]
"""
from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import claude_fingerprints as fp  # noqa: E402

RESPUESTA = json.dumps({
    "id": "msg_1", "type": "message", "role": "assistant",
    "content": [{"type": "text", "text": "ok"}], "model": "claude-opus-5-5",
    "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 1},
}).encode()


class _Sink(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        capturado = {k.lower(): v for k, v in self.headers.items()}
        capturado["_path"] = self.path
        self.server.capturadas.append(capturado)
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(RESPUESTA)))
        self.end_headers()
        self.wfile.write(RESPUESTA)

    def log_message(self, *a):
        pass


def cli_instalado() -> str:
    raiz = pathlib.Path.home() / ".local/share/claude/versions"
    versiones = sorted((p.name for p in raiz.glob("*")), key=lambda s: [int(x) for x in s.split(".") if x.isdigit()])
    if versiones:
        return str(raiz / versiones[-1])
    return str(pathlib.Path.home() / ".local/bin/claude")


def capturar(ruta_cli: str, timeout_s: int = 120) -> dict[str, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", puerto), _Sink)
    server.capturadas = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as config:
        # config aparte: el bloque env de ~/.claude/settings.json fija ANTHROPIC_BASE_URL
        # y ganaria al entorno, que es justo lo que hay que anular para capturar.
        subprocess.run(
            [ruta_cli, "-p", "hi", "--model", "claude-opus-5-5", "--output-format", "json"],
            cwd=config, timeout=timeout_s, capture_output=True,
            env={"HOME": str(pathlib.Path.home()), "PATH": "/usr/bin:/bin",
                 "CLAUDE_CONFIG_DIR": config, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{puerto}",
                 "ANTHROPIC_API_KEY": "sk-ant-capture", "ANTHROPIC_AUTH_TOKEN": ""},
        )
    server.shutdown()
    if not server.capturadas:
        raise SystemExit("el CLI no toco el sink: revisa que la base URL no la fije otro settings")
    return server.capturadas[0]


MANIFIESTO = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# lo que el contrato fija; el cable debe devolverlo byte a byte
CLAVES_CABLE = ("user-agent", "x-app", "x-stainless-arch", "x-stainless-lang", "x-stainless-os",
                "x-stainless-package-version", "x-stainless-retry-count", "x-stainless-runtime",
                "x-stainless-runtime-version", "x-stainless-timeout")


def despliegues_claude() -> list[dict]:
    """Los 8 deployments `claude-acct-*` tal y como estan en el ConfigMap del manifiesto."""
    import yaml
    for doc in yaml.safe_load_all(MANIFIESTO.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            cfg = yaml.safe_load(doc["data"]["config.yaml"])
            return [d for d in cfg["model_list"]
                    if str(d.get("model_info", {}).get("id", "")).startswith("claude-acct-")]
    raise SystemExit("no encuentro el ConfigMap litellm-config")


def cable(timeout_s: int = 240) -> int:
    """Prueba del cable: los 8 deployments contra un sink que hace de api.anthropic.com.

    Corre el Router de litellm — la misma capa de enrutado del proxy — con el bloque
    `headers:` del manifiesto sin tocar, asi que lo que captura el sink es lo que veria
    Anthropic. Hasta aqui el contrato solo verificaba el config con el que arranco el
    proceso (que litellm no reescriba el user-agent ni la familia x-stainless era justo
    lo que habia que medir). Necesita litellm: se corre dentro del pod del proxy.
    """
    from litellm import Router

    deps = despliegues_claude()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        puerto = s.getsockname()[1]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", puerto), _Sink)
    server.capturadas = []
    threading.Thread(target=server.serve_forever, daemon=True).start()

    model_list = []
    for d in deps:
        p = dict(d["litellm_params"])
        # El sink hace de api.anthropic.com y la clave es de mentira (una prueba del cable
        # no gasta cuota de las 8 cuentas), pero con FORMA de la de verdad: con un
        # sk-ant-oat litellm presenta `authorization: Bearer` + la beta oauth, y es justo
        # lo que sale en produccion. Medido el 01-11 en el pod.
        p.update(api_base=f"http://127.0.0.1:{puerto}", api_key="sk-ant-oat01-cable")
        # model_name = el id del deployment: en produccion es el comodin `claude-*` y
        # quien elige cuenta es session_router; aqui interesa la cabecera, no el ruteo.
        model_list.append({"model_name": d["model_info"]["id"], "litellm_params": p})

    router = Router(model_list=model_list, num_retries=0)
    for m in model_list:
        try:
            router.completion(model=m["model_name"], messages=[{"role": "user", "content": "hi"}],
                              max_tokens=8, timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 — si no hay respuesta, abajo falta la cabecera
            print(f"  {m['model_name']}: fallo la peticion ({type(exc).__name__}: {str(exc)[:110]})")
    server.shutdown()

    if len(server.capturadas) != len(deps):
        print(f"el sink recibi {len(server.capturadas)} peticiones, se esperaban {len(deps)} "
              f"(num_retries=0: cada deployment manda una)")
    averiguadas = 0
    for d, capt in zip(deps, server.capturadas):
        dep_id = d["model_info"]["id"]
        esperado = fp.fingerprints()[dep_id]
        dif = {k: (esperado[k], capt.get(k)) for k in CLAVES_CABLE if capt.get(k) != esperado[k]}
        if capt.get("_path") != "/v1/messages":
            dif["_path"] = ("/v1/messages", capt.get("_path"))
        if capt.get("authorization") != "Bearer sk-ant-oat01-cable":
            dif["authorization"] = ("Bearer sk-ant-oat01-cable", capt.get("authorization"))
        if "oauth-2025-04-20" not in capt.get("anthropic-beta", ""):
            dif["anthropic-beta"] = ("…oauth-2025-04-20…", capt.get("anthropic-beta"))
        if dif:
            print(f"  {dep_id}: EN EL CABLE NO COINCIDE " +
                  ", ".join(f"{k} esperado={e!r} capturado={c!r}" for k, (e, c) in dif.items()))
        else:
            averiguadas += 1
            print(f"  {dep_id}: ok en el cable → {capt['user-agent']} · {capt['x-stainless-os']}/"
                  f"{capt['x-stainless-arch']} node {capt['x-stainless-runtime-version']} "
                  f"sdk {capt['x-stainless-package-version']} · {capt['_path']}")
    print(f"\ncable verificado en {averiguadas}/{len(deps)} cuentas "
          f"(host capturado: {server.capturadas[0].get('host') if server.capturadas else '—'})")
    return 0 if averiguadas == len(deps) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--claude", default=None, help="ruta del binario a analizar (por defecto el instalado)")
    ap.add_argument("--cable", action="store_true",
                    help="prueba del egress: los 8 deployments del manifiesto contra un sink "
                         "que hace de api.anthropic.com (necesita litellm; correr en el pod)")
    args = ap.parse_args(argv)

    if args.cable:
        return cable()

    ruta = args.claude or cli_instalado()
    reales = capturar(ruta)
    print(json.dumps({k: v for k, v in sorted(reales.items()) if k.startswith(("x-", "user-agent", "anthropic"))},
                     indent=1))

    declaradas = set(fp.headers(fp.CUENTAS[0][2]))
    faltan = sorted(declaradas - set(reales))
    extra = sorted(k for k in set(reales) - declaradas if k.startswith(("x-stainless", "x-app", "user-agent")))
    version_real = (reales.get("user-agent", "").split("/")[1].split(" ")[0]
                    if reales.get("user-agent", "").startswith("claude-cli/") else "?")
    print(f"\nCLI analizado: {ruta}")
    print(f"version real del CLI: {version_real}   declarada en claude_fingerprints: {fp.CLAUDE_CLI_VERSION}")
    if version_real != fp.CLAUDE_CLI_VERSION:
        print("  → el manifiesto va detras del CLI: actualiza CLAUDE_CLI_VERSION y corre --write")
    if faltan:
        print(f"  → el CLI NO manda claves que nosotros fijamos: {', '.join(faltan)}")
    if extra:
        print(f"  → el CLI manda claves que no estamos fijando: {', '.join(extra)}")
    # el sufijo del UA depende de como se lance el CLI: con API key en config aparte sale
    # "(external, sdk-cli)"; el trafico real de la casa es el CLI interactivo, "(external, cli)".
    # Capturar en el modo que se quiere imitar, o el manifiesto fijara una forma distinta.
    sufijo_real = reales.get("user-agent", "").partition("(")[2].rstrip(")")
    print(f"  nota: sufijo del UA capturado = ({sufijo_real}) — fijamos (external, cli); "
          f"si capturas en modo sdk, no es una discrepancia, es otro modo de lanzamiento")
    return 0 if not faltan and version_real == fp.CLAUDE_CLI_VERSION else 1


if __name__ == "__main__":
    sys.exit(main())
