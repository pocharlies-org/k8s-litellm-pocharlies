"""Captura la cabecera REAL del Claude CLI instalado, para comparar antes de subir version.

Es el paso previo obligado del contrato dgx.litellm.claude-official-headers: antes de
cambiar CLAUDE_CLI_VERSION en dev/claude_fingerprints.py se corre esto con el CLI nuevo
y se mira lo que manda de verdad (user-agent, familia x-stainless-*, anthropic-beta).
Sale 1 si el CLI no llega al sink o si faltan claves que el contrato fija.

Uso:  python3 -I dev/capture_claude_cli_headers.py [--claude <ruta>]
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
        self.server.capturadas.append({k.lower(): v for k, v in self.headers.items()})
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--claude", default=None, help="ruta del binario a analizar (por defecto el instalado)")
    args = ap.parse_args(argv)

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
