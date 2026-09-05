"""El watchdog dice el estado real por capas y sale con el codigo que dice su clase.

POR QUE EXISTE (STA-9, 02-09-2026)
----------------------------------
Dos cosas que se vieron en produccion el mismo dia:

  1. El exit code no se propagaba. El Job `litellm-watchdog-29805960` salio Failed
     con clase `caido` pero el proceso no devolvio exit 1 por contrato: salia con el
     codigo que produjera la excepcion de turno. Un `caido` podia no ponerse rojo en
     ArgoCD, y al reves, un fallo del propio script tampoco tenia codigo asignado.

  2. El 503 que abrio STA-7 era un fallo de ENRUTADO (residente recreandose, Service
     `tooling` sin endpoints, "tooling profile target is unavailable") y el script lo
     trataba igual que un modelo roto, despues de gastar la inferencia.

La arquitectura que lo cierra es de barato a caro y para en la primera capa que
falla: L0 liveliness/readiness de LiteLLM (0 tokens), L1 GET /v1/models del
residente (0 tokens) y L2 la UNICA inferencia real, solo si L0 y L1 estan verdes.
El plan completo esta en el doc de STA-7.

COMO SE PRUEBA, y por que no leyendo el YAML. El contrato es el CODIGO DE SALIDA,
asi que estos tests extraen el script del CronJob y lo EJECUTAN con urllib
parcheado. Buscar cuerdas en el YAML habria pasado por encima de los dos bugs que
motivaron la issue: el `Request(..., timeout=)` que rompia toda corrida (lo pillo
este harness en su primera ejecucion) y el `SystemExit` que no llegaba a ejecutarse.
"""
import ast
import json
import os
import pathlib
import urllib.error
import urllib.request
from unittest import mock

import pytest
import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "k8s" / "litellm-watchdog-cron.yaml"
SA = "/tmp/paperclip-sta9-sa"

# L1 pregunta a los Services del DEPLOYMENT de cada perfil de computo (fix #56):
# el Service de POOL `tooling` no publica 8000 (sus endpoints publican 8888) y
# daba Connection refused con el residente sano. Son los DOS a la vez porque los
# perfiles son exclusivos: uno esta siempre a 0 replicas, y un refused en UNO es
# el estado normal. Condena solo si NINGUNO contesta.
RESIDENTE_LLM_TP = "qwen38-flash-next.llm.svc.cluster.local"
RESIDENTE_CREATIVE = "vllm-qwen38-27b-uncensored.llm.svc.cluster.local"


def _watchdog_script():
    docs = [d for d in yaml.safe_load_all(WATCHDOG.read_text()) if d]
    cron = next(d for d in docs if d.get("kind") == "CronJob")
    container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    blob = "\n".join(container.get("command") or [])
    assert "import json" in blob, "el script del watchdog ha desaparecido del CronJob"
    return blob[blob.index("import json"):]


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url, code, body=b""):
    exc = urllib.error.HTTPError(url, code, "fake", {}, None)
    exc.read = lambda: body
    return exc


def _verde(url):
    if "liveliness" in url:
        return _Resp(b'"I\'m alive!"')
    if "readiness" in url:
        return _Resp({"status": "healthy", "db": "connected"})
    if RESIDENTE_LLM_TP in url or RESIDENTE_CREATIVE in url:
        return _Resp({"data": [{"id": "qwen38-flash-next"}]})
    if "chat/completions" in url:
        return _Resp({"choices": [{"message": {"content": "391"}}]})
    if "refusal_lambda" in url:
        raise _http_error(url, 404)
    if "api.telegram.org" in url:
        return _Resp({"ok": True})
    return None


def _run(handler, estado_previo="ok", token=True):
    """Ejecuta el script del CronJob con urllib parcheado -> (exit_code, urls).

    `handler(url)` devuelve un `_Resp`, una excepcion (se lanza) o None (error de
    conexion). El ConfigMap de estado se resuelve siempre con `estado_previo`.
    """
    src = _watchdog_script().replace(
        "/var/run/secrets/kubernetes.io/serviceaccount", SA)
    os.makedirs(SA, exist_ok=True)
    pathlib.Path(SA + "/namespace").write_text("litellm")
    pathlib.Path(SA + "/token").write_text("tok")
    pathlib.Path(SA + "/ca.crt").write_text(
        pathlib.Path("/etc/ssl/certs/ca-certificates.crt").read_text())

    llamadas = []

    def fake_urlopen(req, timeout=None, context=None, **kw):
        url = req.full_url if hasattr(req, "full_url") else req
        llamadas.append(url)
        if "kubernetes.default.svc" in url:
            return _Resp({"data": {"estado": json.dumps(
                {"clase": estado_previo, "ultimo_aviso": 0})}})
        resultado = handler(url)
        if resultado is None:
            raise urllib.error.URLError("sin backend para " + url)
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    os.environ["LITELLM_MASTER_KEY"] = "k"
    if token:
        os.environ["TELEGRAM_BOT_TOKEN"] = "tok-env"
        os.environ["TELEGRAM_CHAT_ID"] = "33285833"
    else:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    exito = None
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
        try:
            exec(compile(src, "<watchdog>", "exec"), {})  # noqa: S102
        except SystemExit as exc:
            exito = exc.code
    assert exito is not None, (
        "el script termino sin SystemExit: el Job sale con el codigo que produzca "
        "la excepcion de turno, que es el bug de STA-9")
    return exito, llamadas


# ── 1. capas: la inferencia es la ULTIMA comprobacion, no la primera ────────────

def test_todo_verde_gasta_UNA_inferencia_y_sale_con_cero():
    exito, llamadas = _run(_verde)
    assert exito == 0
    assert sum("chat/completions" in u for u in llamadas) == 1, (
        "L2 es la unica capa que gasta tokens; duplicarla es volver al estado en "
        "que el semaforo caro se comia el presupuesto y encima mentia")


def test_L0_roto_es_caido_sin_gastar_la_inferencia():
    def handler(url):
        return None if "liveliness" in url else _verde(url)
    exito, llamadas = _run(handler)
    assert exito == 1, "caido tiene que poner el Job en Failed"
    assert not any("chat/completions" in u for u in llamadas)


def test_L1_los_dos_residentes_caidos_es_caido_sin_gastar_la_inferencia():
    """El 503 del incidente que abrio STA-7, reproducido en la capa que lo ve.

    Desde #56 hay DOS residentes y solo condena si NINGUNO contesta, asi que el
    503 se le echa a los dos: es el estado "ambos perfiles a 0 replicas", lo
    unico de L1 que es un fallo.
    """
    def handler(url):
        if RESIDENTE_LLM_TP in url or RESIDENTE_CREATIVE in url:
            raise _http_error(
                url, 503, b'{"error": "tooling profile target is unavailable"}')
        return _verde(url)
    exito, llamadas = _run(handler)
    assert exito == 1
    assert not any("chat/completions" in u for u in llamadas), (
        "si el residente no publica, la inferencia no se paga: L1 existe para eso")


def test_L1_un_solo_residente_caido_no_es_caido():
    """Los perfiles de computo son EXCLUSIVOS: el inactivo esta siempre a 0
    replicas y su connection refused es el estado normal, no un fallo. Condenar
    con uno solo caido es exactamente el falso positivo que cerraba #56 (Job
    Failed cada 10 min con el residente sano, tapando un Degraded de verdad)."""
    def handler(url):
        if RESIDENTE_CREATIVE in url:
            return None
        return _verde(url)
    exito, llamadas = _run(handler)
    assert exito == 0, (
        "condena con un solo residente caido: vuelve el bug del Service de pool "
        "muerto; solo NINGUN residente contestando es un fallo")
    assert any("chat/completions" in u for u in llamadas), (
        "con el residente del perfil activo vivo, L2 es la comprobacion que toca")


def test_L1_dice_el_cuerpo_del_error_no_solo_el_codigo():
    """`str(HTTPError)` es "HTTP Error 503: Service Unavailable": no dice si el
    residente no tiene endpoints o si lo estamos bloqueando nosotros, y esas dos
    cosas se arreglan en sitios distintos. El cuerpo tiene que llegar al log.

    Desde #56 el bucle de RESIDENTES atrapaba el error de CADA residente con un
    `except Exception` interno que guardaba `str(exc)` - sin cuerpo - y dejaba
    inalcanzable el `except HTTPError` exterior que si propagaba
    `cuerpo_error(exc)`. Quedo documentado aqui con un xfail estricto; se reparo
    en el bucle (el HTTPError se captura en el propio bucle y el truncado es
    sobre el CUERPO, no sobre el mensaje entero) y este test afirma la
    reparacion: el cuerpo del 503 en el aviso, no solo el codigo.
    En L2 el mismo contrato sigue vivo: ver test_inferencia_vacia_con_200...
    """
    def handler(url):
        if RESIDENTE_LLM_TP in url:
            raise _http_error(
                url, 503, b'{"error": "tooling profile target is unavailable"}')
        if RESIDENTE_CREATIVE in url:
            return None
        return _verde(url)
    with mock.patch("builtins.print") as salida:
        exito, _ = _run(handler)
    assert exito == 1, (
        "con los dos residentes caidos el Job tiene que ponerse rojo pase lo que "
        "pase con el cuerpo del error")
    log = "\n".join(str(a) for c in salida.call_args_list for a in c.args)
    assert "HTTP Error 503" not in log, (
        "el aviso lleva el mensaje generico de str(HTTPError): el except "
        "interno ha vuelto a tragarse el HTTPError antes de extraer el cuerpo")
    assert "HTTP 503" in log, (
        "L1 ni siquiera reporta el codigo: el aviso no dice que capa del "
        "enrutado fallo")
    assert "tooling profile target is unavailable" in log, (
        "el cuerpo del error HTTP no llega al log: con solo el codigo no se "
        "distingue un residente sin endpoints de un bloqueo nuestro")


def test_L1_pregunta_al_residente_no_al_catalogo_de_litellm():
    """El catalogo de LiteLLM sale del ConfigMap y se publica aunque el residente
    este a 0 replicas (auditoria del 19-08 con los alias -uncensored). Preguntarle
    a LiteLLM por sus modelos diria "si" justo cuando no hay nadie detras.

    Desde #56 ademas NO es el Service de pool `tooling` (publica 8888, no 8000):
    pregunta al Service del DEPLOYMENT de cada perfil, a los dos."""
    _, llamadas = _run(_verde)
    modelos = [u for u in llamadas if "/v1/models" in u]
    assert any(RESIDENTE_LLM_TP in u for u in modelos)
    assert any(RESIDENTE_CREATIVE in u for u in modelos)
    assert not any("tooling.llm.svc" in u for u in llamadas), (
        "L1 ha vuelto a preguntar al Service de pool, que no publica 8000")


def test_readiness_rota_no_es_caido():
    """El proxy sirve con postgres caido a proposito (allow_requests_on_db_unavailable).
    Pintar rojo un LiteLLM que esta sirviendo fue el error del 20-07 con readiness."""
    def handler(url):
        return None if "readiness" in url else _verde(url)
    assert _run(handler)[0] == 0


# ── 2. el codigo de salida en TODAS las ramas ───────────────────────────────────

def test_inferencia_vacia_con_200_es_caido():
    def handler(url):
        if "chat/completions" in url:
            return _Resp({"choices": [{"message": {"content": ""}}]})
        return _verde(url)
    assert _run(handler)[0] == 1


def test_inferencia_que_no_es_391_es_caido():
    def handler(url):
        if "chat/completions" in url:
            return _Resp({"choices": [{"message": {"content": "390"}}]})
        return _verde(url)
    assert _run(handler)[0] == 1


def test_cuota_agotada_no_es_caido_y_no_pone_el_job_rojo():
    """Regla del 15-08: con la cuota agotada el cluster esta bien, y un Job rojo
    cada 10 min durante dias taparia un Degraded de verdad."""
    def handler(url):
        if "chat/completions" in url:
            raise _http_error(
                url, 429,
                b'{"error": {"code": "usage_limit_reached", "resets_at": 1789000000}}')
        return _verde(url)
    assert _run(handler)[0] == 0


def test_abliterado_no_pone_el_job_rojo():
    def handler(url):
        if "refusal_lambda" in url:
            return _Resp({"lambda": 2.5})
        return _verde(url)
    assert _run(handler)[0] == 0


def test_caido_con_telegram_roto_sigue_saliendo_con_uno():
    """El Job fallido es la senal para ArgoCD aunque el mensaje no llegue."""
    def handler(url):
        if "liveliness" in url or "api.telegram.org" in url:
            return None
        return _verde(url)
    assert _run(handler)[0] == 1


def test_caido_sin_token_de_telegram_sigue_saliendo_con_uno():
    """Antes del 02-09 esta rama existia pero el contrato no estaba escrito: sin
    token el aviso no sale, y aun asi el Job tiene que ponerse rojo."""
    def handler(url):
        return None if "liveliness" in url else _verde(url)
    assert _run(handler, token=False)[0] == 1


def test_una_excepcion_del_propio_watchdog_sale_con_uno():
    """Un watchdog que muere con una excepcion propia no puede salir con 0: en
    ArgoCD el silencio se lee como "todo bien"."""
    def handler(url):
        if "chat/completions" in url:
            return _Resp("esto-no-es-el-formato-esperado")
        return _verde(url)
    assert _run(handler)[0] == 1


# ── 3. el contrato de salida, leido del arbol ───────────────────────────────────

def test_ultimo_statement_del_script_es_un_system_exit_con_salida():
    """El bug de la linea ~291: si el ultimo statement del bloque no es un raise,
    al caer al final del todo se sale con 0 por defecto."""
    tree = ast.parse(_watchdog_script())
    trybloques = [n for n in tree.body if isinstance(n, ast.Try)]
    assert trybloques, "el cuerpo del script no envuelve nada: no hay contrato de salida"
    final = [c for c in trybloques[-1].body if isinstance(c, (ast.Raise, ast.If))][-1]
    while isinstance(final, ast.If):
        final = (final.body + final.orelse)[-1]
    assert isinstance(final, ast.Raise) and isinstance(final.exc, ast.Call)
    assert getattr(final.exc.func, "id", "") == "SystemExit"
    assert getattr(final.exc.args[0], "id", "") == "SALIDA", (
        "el final tiene que salir con SALIDA, que vale 1 solo si la clase es caido")
    assert any(
        getattr(h.type, "id", "") == "SystemExit" for h in trybloques[-1].handlers
    ), "SystemExit tiene que re-lanzarse: tragarselo es el bug del exit code"


def test_la_rama_de_error_del_watchdog_sale_con_uno():
    """El `except BaseException` tiene que salir con 1, no con un literal suelto.

    Se acepta `SIN_CLASE` (la constante declarada arriba, 1 a proposito) o el 1
    literal; lo que no puede es salir con 0 o sin raise, que es como se colaba
    el silencio en ArgoCD.
    """
    tree = ast.parse(_watchdog_script())
    trybloques = [n for n in tree.body if isinstance(n, ast.Try)]
    handler = next(
        h for h in trybloques[-1].handlers
        if getattr(h.type, "id", "") == "BaseException")
    finales = [c for c in handler.body if isinstance(c, (ast.Raise, ast.Try))]
    assert finales, "la rama de error no lanza nada: sale con 0 por defecto"
    ultimo = [c for c in finales[-1].body if isinstance(c, ast.Raise)][-1] \
        if isinstance(finales[-1], ast.Try) else finales[-1]
    assert isinstance(ultimo, ast.Raise) and isinstance(ultimo.exc, ast.Call)
    assert getattr(ultimo.exc.func, "id", "") == "SystemExit"
    arg = ultimo.exc.args[0]
    valor = arg.id if isinstance(arg, ast.Name) else getattr(arg, "value", None)
    assert valor == "SIN_CLASE", (
        f"el error del watchdog sale con {valor!r}: tiene que salir con la constante "
        "SIN_CLASE, no con un literal. Dos literales de exit code en el mismo script "
        "es exactamente como se pierde el contrato.")
