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
  --tls          el otro indicio, el que no arregla el YAML: JA3/JA4 del ClientHello.
                 Mide el CLI nativo y litellm con el mismo sink TLS (ver tls()).
                 Medido 2026-11-01 — divergen por completo: CLI (Node/undici) JA3
                 1b68cea258d57ce22f08d83af5d91c56 / JA4 t13d181100_5d04281c6031_dc7e9eda0726
                 sin ALPN; litellm (httpx) JA3 84271532a49d4795439fe0addea3b46d / JA4
                 t13d1114h1_01855bbd05ee_6c778d45a497 con ALPN http/1.1.

Uso:  python3 -I dev/capture_claude_cli_headers.py [--claude <ruta>] [--cable]
      python3 -I dev/capture_claude_cli_headers.py --tls [cli|litellm|ambos]
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import claude_fingerprints as fp  # noqa: E402

ANFITRION = "api.anthropic.com"   # el nombre real: el SNI tiene que ser este en los dos lados

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


# ── TLS: JA3 / JA4 del egress ────────────────────────────────────────────────
# Las cabeceras las fija el YAML; el ClientHello no: lo construye la libreria de cada
# lado (httpx en litellm, Node/undici en el CLI). Se mide con el mismo instrumento en
# los dos: un sink que hace de api.anthropic.com con certificado propio y se queda con
# el ClientHello crudo antes del handshake, con el nombre de verdad en el SNI (el DNS se
# parchea, la pila TLS no). Especificacion FoxIO JA4+: JA3 = MD5 de
# version,cifrados,extensiones,curvas,formatos-de-punto en orden; JA4 = prefijo
# (proto/version/SNI/numeros/ALPN) + SHA256[0:12] de los cifrados ordenados +
# SHA256[0:12] de las extensiones ordenadas (sin SNI ni ALPN) con las signature_algorithms
# detras. GREASE fuera de todo.
_VER_JA4 = {0x0200: "s2", 0x0300: "s3", 0x0301: "10", 0x0302: "11", 0x0303: "12",
            0x0304: "13", 0x0305: "15", 0x0306: "16"}


def _es_grease(v: int) -> bool:
    return (v & 0x0F0F) == 0x0A0A


def _lista_2(b: bytes) -> list[int]:
    return [int.from_bytes(b[i:i + 2], "big") for i in range(0, len(b) - 1, 2)]


def _lee_clienthello(b: bytes) -> dict:
    """Los campos que necesitan JA3 y JA4, sacados de un registro Handshake (0x16)."""
    if len(b) < 6 or b[0] != 0x16 or b[5] != 0x01:
        raise ValueError("no es un ClientHello (registro 0x16 con handshake 0x01)")
    cur = 5 + 4                                    # cabecera de registro + tipo y longitud
    version = int.from_bytes(b[cur:cur + 2], "big"); cur += 2
    cur += 32                                      # random
    cur += 1 + b[cur]                              # session id
    n = int.from_bytes(b[cur:cur + 2], "big"); cur += 2
    cifrados = _lista_2(b[cur:cur + n]); cur += n
    m = b[cur]; cur += 1
    cur += m                                       # compression methods
    ne = int.from_bytes(b[cur:cur + 2], "big"); cur += 2
    extensiones: dict[int, bytes] = {}
    orden: list[int] = []
    fin = cur + ne
    while cur + 4 <= fin:
        et = int.from_bytes(b[cur:cur + 2], "big")
        el = int.from_bytes(b[cur + 2:cur + 4], "big")
        extensiones[et] = b[cur + 4:cur + 4 + el]
        orden.append(et)
        cur += 4 + el

    sni = ""
    if 0 in extensiones:
        largo = int.from_bytes(extensiones[0][3:5], "big")
        sni = extensiones[0][5:5 + largo].decode("ascii", "replace")
    alpn = []
    if 16 in extensiones:
        p, fin_a = 2, int.from_bytes(extensiones[16][:2], "big")
        while p < 2 + fin_a:
            l = extensiones[16][p]
            alpn.append(extensiones[16][p + 1:p + 1 + l].decode("ascii", "replace"))
            p += 1 + l
    # cada lista lleva su propia longitud delante: 2 bytes en grupos y sigalgs, 1 en
    # supported_versions (ec_point_formats es de 1 byte por elemento, ver abajo)
    return {
        "version": version,
        "cifrados": cifrados,
        "extensiones": orden,
        "curvas": _lista_2(extensiones.get(10, b"")[2:]),
        "punto": list(extensiones.get(11, b"")[1:]) if 11 in extensiones else [],
        "sigalgs": _lista_2(extensiones.get(13, b"")[2:]),
        "soportadas": _lista_2(extensiones.get(43, b"")[1:]),
        "sni": sni,
        "alpn": alpn,
    }


def ja3(h: dict) -> tuple[str, str]:
    """(cadena legible, hash MD5) — la cadena es lo que hay que documentar si diverge."""
    grupos = [
        str(h["version"]),
        ",".join(str(x) for x in h["cifrados"] if not _es_grease(x)),
        ",".join(str(x) for x in h["extensiones"] if not _es_grease(x)),
        ",".join(str(x) for x in h["curvas"]),
        ",".join(str(x) for x in h["punto"]),
    ]
    cadena = ",".join(grupos)
    return cadena, hashlib.md5(cadena.encode()).hexdigest()


def ja4(h: dict) -> tuple[str, str]:
    """(fingerprint, secciones en claro) — el prefijo explica el hash, no lo adivines."""
    c = sorted(x for x in h["cifrados"] if not _es_grease(x))
    e_todo = [x for x in h["extensiones"] if not _es_grease(x)]
    e = sorted(x for x in e_todo if x not in (0, 16))     # SNI y ALPN fuera del hash
    version = max(h["soportadas"], default=h["version"])
    if not h["sni"]:
        marca = "nn"
    else:
        try:
            ipaddress.ip_address(h["sni"])
            marca = "i"
        except ValueError:
            marca = "d"
    alpn = [c2 for a in h["alpn"] for c2 in a if c2.isalnum()]
    prefijo = f"t{_VER_JA4.get(version, '??')}{marca}{len(c):02d}{len(e_todo):02d}" \
              f"{(alpn[0] + alpn[-1]).lower() if alpn else '00'}"
    cif = ",".join(f"{x:04x}" for x in c)
    ext = ",".join(f"{x:04x}" for x in e)
    sig = ",".join(f"{x:04x}" for x in h["sigalgs"])
    claro = f"{prefijo}_{cif}_{ext}_{sig}"
    hashb = hashlib.sha256(cif.encode()).hexdigest()[:12] if cif else "000000000000"
    hashe = (hashlib.sha256("_".join(p for p in (ext, sig) if p).encode()).hexdigest()[:12]
             if (ext or sig) else "000000000000")
    return f"{prefijo}_{hashb}_{hashe}", claro


def _certificado(d: pathlib.Path) -> str:
    """Autofirmado para api.anthropic.com: el cliente verifica contra él, sin desactivar TLS."""
    crt, clave = d / "crt.pem", d / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(clave),
         "-out", str(crt), "-days", "2", "-subj", "/CN=api.anthropic.com",
         "-addext", "subjectAltName=DNS:api.anthropic.com"],
        check=True, capture_output=True)
    return str(crt)


def _registro_tls(sock, espera_s: float = 2.0) -> bytes:
    """Primer registro TLS por MSG_PEEK, sin consumir: el handshake sigue funcionando.

    El ClientHello puede llegar troceado por TCP (padding mediante): se espera a que este
    entero o a que dejen de llegar bytes, si no el parser lee fuera del registro.
    """
    visto, hasta = b"", time.monotonic() + espera_s
    while time.monotonic() < hasta:
        try:
            b = sock.recv(65536, socket.MSG_PEEK)
        except OSError:
            b = b""
        if len(b) >= 5 and len(b) >= int.from_bytes(b[3:5], "big") + 5:
            return b[:int.from_bytes(b[3:5], "big") + 5]
        if len(b) > len(visto):
            visto = b
            continue
        time.sleep(0.05)
    return visto


class _SinkTLS(http.server.ThreadingHTTPServer):
    """Hace de api.anthropic.com por TLS y guarda el ClientHello de cada conexion."""

    daemon_threads = True

    def __init__(self, addr, ctx):
        self.ctx = ctx
        self.clienthellos = []
        self.capturadas = []                     # _Sink do_POST escribe aqui
        super().__init__(addr, _Sink)

    def get_request(self):
        cruda, direccion = self.socket.accept()
        hola = _registro_tls(cruda)
        if hola:
            self.clienthellos.append(hola)
        return self.ctx.wrap_socket(cruda, server_side=True), direccion


def _informa(server, resultados: dict, lado: str) -> None:
    """Traduce el ultimo ClientHello recibido a JA3/JA4 y lo pinta con las secciones en claro."""
    if not server.clienthellos:
        print(f"[{lado}] no llego ningun ClientHello al sink")
        return
    h = _lee_clienthello(server.clienthellos.pop())
    cadena, hash3 = ja3(h)
    huella, claro = ja4(h)
    resultados[lado] = {"ja3": hash3, "ja4": huella}
    print(f"\n[{lado}]  SNI={h['sni']}  ALPN={h['alpn']}")
    print(f"  JA3  {hash3}")
    print(f"       {cadena}")
    print(f"  JA4  {huella}")
    print(f"       {claro}")


def tls(cliente: str = "ambos", timeout_s: int = 180, puerto: int = 0) -> int:
    """JA3/JA4 del egress: el del CLI nativo y el de litellm, medidos igual en los dos lados.

    shortcut: para medir el CLI hay que escuchar en el 443 (`--puerto 443`, con sudo): el CLI
    se queda colgado si ANTHROPIC_BASE_URL lleva un puerto no estandar (medido el 01-11 en el
    x86 — con https://api.anthropic.com:41234 no abre conexion). litellm no lo padece.
    """
    import ssl

    resultados: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        crt = _certificado(tmp)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, str(tmp / "key.pem"))
        try:
            server = _SinkTLS(("127.0.0.1", puerto), ctx)
        except PermissionError:
            print(f"el puerto {puerto} necesita privilegios: correr con sudo")
            return 1
        puerto = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"https://{ANFITRION}" if puerto == 443 else f"https://{ANFITRION}:{puerto}"

        if cliente in ("cli", "ambos"):
            # El CLI ignora NODE_OPTIONS (medido el 01-11: con --require el preload no llega a
            # ejecutarse y la peticion se va a la Anthropic real), asi que el nombre se desvia
            # por /etc/hosts, que glibc si obedece. Ventana de segundos, restaurado al salir.
            hosts = pathlib.Path("/etc/hosts")
            try:
                original = hosts.read_text()
                hosts.write_text(f"{original}\n127.0.0.1 {ANFITRION}\n")
            except OSError as exc:
                print(f"no puedo escribir /etc/hosts ({exc.strerror}): el lado del CLI necesita "
                      f"sudo y --puerto 443")
                return 1
            config = str(tmp / "cli")
            pathlib.Path(config).mkdir()
            try:
                subprocess.run(
                    [cli_instalado(), "-p", "hi", "--model", "claude-opus-5-5",
                     "--output-format", "json"],
                    cwd=config, timeout=timeout_s, capture_output=True,
                    stdin=subprocess.DEVNULL,
                    env={"HOME": str(pathlib.Path.home()), "PATH": "/usr/bin:/bin",
                         "CLAUDE_CONFIG_DIR": config, "ANTHROPIC_BASE_URL": base,
                         "ANTHROPIC_API_KEY": "sk-ant-capture", "ANTHROPIC_AUTH_TOKEN": "",
                         "NODE_EXTRA_CA_CERTS": crt})
            except FileNotFoundError:
                print(f"no encuentro el CLI en {cli_instalado()} — pasar --claude <ruta> "
                      f"(con sudo, HOME apunta a root y no esta el de tu usuario)")
                return 1
            except subprocess.TimeoutExpired as exc:
                # el ClientHello ya esta capturado aunque el CLI se quede reintentando:
                # importa la huella, no que la prueba salga limpia.
                err = (exc.stderr or b"").decode("utf-8", "replace").strip()
                print(f"[claude-cli] el CLI no cerro en {timeout_s}s "
                      f"(se mide el ClientHello igualmente)")
                if err:
                    print(f"  stderr del CLI: {err[-300:]}")
            finally:
                hosts.write_text(original)

        if cliente in ("litellm", "ambos"):
            os.environ["SSL_CERT_FILE"] = crt
            real = socket.getaddrinfo
            socket.getaddrinfo = lambda h, *a, **k: (
                real("127.0.0.1", *a, **k) if h == "api.anthropic.com" else real(h, *a, **k))
            try:
                from litellm import Router
                p = dict(despliegues_claude()[0]["litellm_params"])
                p.update(api_base=base, api_key="sk-ant-oat01-tls")
                Router(model_list=[{"model_name": "tls", "litellm_params": p}],
                       num_retries=0).completion(
                    model="tls", messages=[{"role": "user", "content": "hi"}],
                    max_tokens=8, timeout=timeout_s)
            except ImportError:
                print("litellm no esta aqui: el lado del proxy se mide dentro del pod")
            except Exception as exc:  # noqa: BLE001 — interesa el ClientHello, no la respuesta
                print(f"[litellm] fallo la peticion ({type(exc).__name__}: {str(exc)[:100]}), "
                      f"se mide el ClientHello igualmente")
            finally:
                socket.getaddrinfo = real
        server.shutdown()
        if cliente in ("cli", "ambos"):
            _informa(server, resultados, "claude-cli")
        if cliente in ("litellm", "ambos"):
            _informa(server, resultados, "litellm")

    lados = list(resultados)
    if len(resultados) == 2:
        a, b = (resultados[x] for x in lados)
        print(f"\ndivergencia: JA3 {'IGUAL' if a['ja3'] == b['ja3'] else 'DISTINTO'} · "
              f"JA4 {'IGUAL' if a['ja4'] == b['ja4'] else 'DISTINTO'}")
        if a["ja3"] != b["ja3"]:
            print("  el TLS del proxy NO se parece al del CLI: es un indicio real, "
                  "no arreglable en el manifiesto (lo fija httpx, no el YAML)")
    return 0 if resultados else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--claude", default=None, help="ruta del binario a analizar (por defecto el instalado)")
    ap.add_argument("--cable", action="store_true",
                    help="prueba del egress: los 8 deployments del manifiesto contra un sink "
                         "que hace de api.anthropic.com (necesita litellm; correr en el pod)")
    ap.add_argument("--tls", nargs="?", const="ambos", choices=("cli", "litellm", "ambos"),
                    help="JA3/JA4 del ClientHello: el del CLI nativo y/o el de litellm, "
                         "medidos con el mismo sink TLS")
    ap.add_argument("--puerto", type=int, default=0,
                    help="donde escucha el sink TLS (0 = uno libre; el CLI necesita el 443)")
    args = ap.parse_args(argv)

    if args.cable:
        return cable()
    if args.tls:
        return tls(args.tls, puerto=args.puerto)

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
