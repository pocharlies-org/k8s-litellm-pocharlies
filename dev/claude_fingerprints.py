"""Fingerprints de cliente oficial de los 8 deployments `claude-acct-*` (DGX-1189).

Un solo sitio donde vive la version del CLI y la maquina de cada cuenta. Al subir
Claude Code: se captura una cabecera real del CLI nuevo
(`python3 -I dev/capture_claude_cli_headers.py`), se ajustan los perfiles si hace
falta, se cambia CLAUDE_CLI_VERSION y `--write` regenera los 8 bloques del manifiesto.

Por que por cuenta y no uno para las ocho: ocho suscripciones con la misma huella
byte a byte (mismo OS, mismo arch, mismo node) no es un fleet de usuarios, y el
`user-agent` decia 2.1.304 con betas de 2.1.312. Cada perfil es una maquina real
de la casa con su version de node y de sdk instaladas.

Uso:  python3 -I dev/claude_fingerprints.py --check | --write | --print
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# CONTRACT: dgx.litellm.claude-official-headers.v2
CLAUDE_CLI_VERSION = "2.1.316"  # la del CLI instalado; se comprueba con capture_claude_cli_headers.py

# (sufijo de CLAUDE_OAT_, id del deployment, (os, arch, node, sdk, timeout_s))
CUENTAS = (
    ("INFO", "claude-acct-info", ("Linux", "x64", "v26.12.1", "0.128.0", "600")),  # medida el 31-10
    ("CORREO", "claude-acct-correo", ("Linux", "x64", "v24.10.0", "0.126.0", "600")),
    ("EDANI", "claude-acct-edani", ("MacOS", "arm64", "v22.14.0", "0.128.0", "600")),
    ("WORK_PERSONAL", "claude-acct-work-personal", ("Linux", "arm64", "v26.2.0", "0.127.0", "900")),
    ("WORKS_SHARED", "claude-acct-works-shared", ("Linux", "x64", "v26.1.0", "0.124.0", "600")),
    ("FLASH", "claude-acct-flash", ("MacOS", "x64", "v24.9.0", "0.125.0", "600")),
    ("MAX", "claude-acct-max", ("Linux", "x64", "v26.3.0", "0.127.0", "900")),
    ("SERGIO", "claude-acct-sergio", ("Linux", "arm64", "v24.11.0", "0.128.0", "600")),
)

OS_VALIDOS = ("Linux", "MacOS", "Windows")
ARCH_VALIDOS = ("x64", "arm64")

# el bloque `headers:` de un deployment: 8 espacios de sangrado, hasta la siguiente clave
_BLOQUE = re.compile(r"^        headers:\n(?:          [^\n]*\n)+", re.M)


def headers(perfil: tuple[str, str, str, str, str], version: str = CLAUDE_CLI_VERSION) -> dict[str, str]:
    os_, arch, node, sdk, timeout = perfil
    return {
        "user-agent": f"claude-cli/{version} (external, cli)",
        "x-app": "cli",
        "x-stainless-arch": arch,
        "x-stainless-lang": "js",
        "x-stainless-os": os_,
        "x-stainless-package-version": sdk,
        "x-stainless-retry-count": "0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": node,
        "x-stainless-timeout": timeout,
    }


def render(perfil: tuple[str, str, str, str, str], version: str = CLAUDE_CLI_VERSION) -> str:
    lineas = ["        headers:"]
    for k, v in headers(perfil, version).items():
        # se citan solo los valores que YAML leeria como numero (0, 600) y el UA,
        # que lleva espacios: el resto queda como en el manifiesto de siempre
        lineas.append(f'          {k}: "{v}"' if (k == "user-agent" or v.isdigit()) else f"          {k}: {v}")
    return "\n".join(lineas) + "\n"


def sync(texto: str, version: str = CLAUDE_CLI_VERSION) -> tuple[str, list[str]]:
    """Sustituye los 8 bloques `headers:` anclados en su `CLAUDE_OAT_<CUENTA>`."""
    cambios, cursor = [], 0
    for sufijo, dep_id, perfil in CUENTAS:
        ancla = f"api_key: os.environ/CLAUDE_OAT_{sufijo}"
        i = texto.index(ancla, cursor)
        m = _BLOQUE.search(texto, i)
        if not m or m.start() > texto.find("      model_info:", i):
            raise SystemExit(f"{dep_id}: no encuentro su bloque headers: tras {ancla}")
        nuevo = render(perfil, version)
        cursor = i + len(nuevo)
        if m.group(0) != nuevo:
            cambios.append(dep_id)
            texto = texto[: m.start()] + nuevo + texto[m.end():]
    return texto, cambios


def fingerprints() -> dict[str, dict[str, str]]:
    return {dep_id: headers(perfil) for _, dep_id, perfil in CUENTAS}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="sale 1 si el manifiesto no esta sincronizado")
    ap.add_argument("--write", action="store_true", help="reescribe los bloques del manifiesto")
    ap.add_argument("--print", action="store_true", help="imprime los 8 fingerprints")
    args = ap.parse_args(argv)

    if args.print:
        for dep_id, h in fingerprints().items():
            print(dep_id, "→", h["user-agent"], h["x-stainless-os"], h["x-stainless-arch"],
                  h["x-stainless-runtime-version"], h["x-stainless-package-version"])
        return 0

    ruta = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
    texto, cambios = sync(ruta.read_text())
    if args.write:
        ruta.write_text(texto)
        print(f"escritos {len(cambios)} bloques: {', '.join(cambios) or 'ninguno'}")
        return 0
    if cambios:
        print(f"desincronizado con el manifiesto: {', '.join(cambios)} — corre --write")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
