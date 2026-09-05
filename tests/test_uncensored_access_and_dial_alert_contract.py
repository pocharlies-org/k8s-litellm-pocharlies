"""Quien puede pedir la ruta abliterada, y quien avisa si el dial global se mueve.

POR QUE EXISTEN LAS DOS PIEZAS Y POR QUE EN EL MISMO FICHERO. Una auditoria del
19-08 encontro que `/admin/refusal_lambda` no tiene autenticacion y que **las
NetworkPolicies de este cluster no se aplican**: se comprobo contra cuatro
namespaces con `default-deny-ingress` y los cuatro aceptaban conexiones desde otro
namespace. O sea que la recomendacion obvia —una netpol— habria sido un no-op mas.

Como no se puede PREVENIR por red, se hacen las dos cosas que si se pueden:

  1. **Reducir quien puede pedirla.** Antes, cualquier key del proxy: la del gateway
     de OpenClaw tiene `models: []` y su equipo tambien, asi que lo unico que
     mantenia censurado el trafico normal era que nadie pedia el alias por su
     nombre. Una convencion, no un control. Ahora la lista esta en git.
  2. **Detectar el dial global.** Ya paso: quedo en λ=2.5 durante horas afectando a
     TODO el trafico, y lo unico que lo devolvio a 0 fue un reinicio del pod.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
WATCHDOG = ROOT / "k8s" / "litellm-watchdog-cron.yaml"

WANT_FN = {"_uncensored_access_denied"}
WANT_CONST = {"UNCENSORED_ALLOWED_KEY_ALIASES", "UNCENSORED_GATED_ALIASES",
              "TOOLING_UNCENSORED_ALIASES", "TOOLING_UNCENSORED_MODE_TARGETS"}


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in yaml.safe_load_all(MANIFEST.read_text())
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def hook():
    tree = ast.parse(_hook_source())
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("gatepure")
    mod.os = __import__("os")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


def _watchdog_script():
    docs = [d for d in yaml.safe_load_all(WATCHDOG.read_text()) if d]
    cron = next(d for d in docs if d.get("kind") == "CronJob")
    spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    for container in spec["containers"]:
        blob = "\n".join(container.get("command") or []) + "\n" + "\n".join(
            container.get("args") or [])
        if "import json" in blob:
            src = blob[blob.index("import json"):]
            for end in ("\nPY\n", "\nEOF\n"):
                if end in src:
                    src = src[: src.index(end)]
            return src
    raise AssertionError("no encuentro el script del watchdog")


# ── 1. el gate de acceso ────────────────────────────────────────────────────────

def test_an_unlisted_key_cannot_request_the_abliterated_route(hook):
    denied, detail = hook._uncensored_access_denied(
        "tooling-uncensored", "una-key-cualquiera")
    assert denied is True
    assert detail["requested"] == "tooling-uncensored"
    assert detail["key"] == "una-key-cualquiera"


def test_the_gate_also_covers_the_DIRECT_names(hook):
    """Si solo cubriera el alias de capacidad, se rodearia pidiendo el directo."""
    assert set(hook.TOOLING_UNCENSORED_MODE_TARGETS.values()) <= hook.UNCENSORED_GATED_ALIASES
    for direct in hook.TOOLING_UNCENSORED_MODE_TARGETS.values():
        denied, _ = hook._uncensored_access_denied(direct, "una-key-cualquiera")
        assert denied is True, direct


def test_the_censored_route_is_NOT_gated(hook):
    """Esto no puede convertirse en un gate sobre el trafico normal."""
    for alias in ("tooling", "high", "max", "deepseek-v4-flash-0731"):
        denied, _ = hook._uncensored_access_denied(alias, "una-key-cualquiera")
        assert denied is False, alias


def test_the_current_consumer_keeps_working(hook):
    """El gate no puede tirar al unico consumidor real: seria un corte, no un
    control. La key del gateway sigue en la lista hasta que exista una dedicada."""
    for alias in ("tooling-uncensored",
                  *hook.TOOLING_UNCENSORED_MODE_TARGETS.values()):
        denied, _ = hook._uncensored_access_denied(alias, "openclaw-qwen36-prod")
        assert denied is False, alias


def test_the_default_allowlist_is_NOT_empty(hook):
    """Vacio significa "sin restriccion". Que el default no lo sea es el control."""
    assert hook.UNCENSORED_ALLOWED_KEY_ALIASES
    assert "openclaw-qwen36-prod" in hook.UNCENSORED_ALLOWED_KEY_ALIASES


def test_the_gate_runs_before_anything_touches_the_backend():
    """Un gate que corre despues de resolver el alias ya ha hecho trabajo por una
    peticion que va a rechazar, y es mas facil de rodear."""
    src = _hook_source()
    body = src[src.index("async def async_pre_call_hook"):]
    pos_gate = body.index("_uncensored_access_denied")
    for despues in ("_sanitize_sampling", "cap_alias = (", "_resolve_tooling"):
        assert pos_gate < body.index(despues), despues


# ── 2. la alerta del dial global ────────────────────────────────────────────────

def test_the_watchdog_reads_the_global_dial():
    src = _watchdog_script()
    assert "/admin/refusal_lambda" in src
    assert "DIALES" in src
    # Los runtimes que pueden llevar la proyeccion. Se ancla a la CLAVE de cada
    # entrada de DIALES, no al hostname: el watchdog parte el host en dos lineas
    # para no pasar del ancho YAML, y un literal contiguo dejaria de casar sin
    # que el watchdog hubiera cambiado (fue justo como este test se quedo viejo).
    bloque = src[src.index("DIALES = {"):src.index("dial_alto = []")]
    for nombre in ("qwen38-flash-next",
                   "deepseek-v4-flash-0731",
                   "qwen38-27b"):
        assert '"%s"' % nombre in bloque, nombre


def test_a_head_that_does_not_answer_is_NOT_a_failure():
    """Los dos perfiles son exclusivos, asi que uno de los backends esta siempre a
    0 replicas. Si eso contara como fallo, el watchdog gritaria cada 10 minutos y
    dejaria de significar nada."""
    src = _watchdog_script()
    bloque = src[src.index("DIALES = {"):src.index("--- clase del incidente")]
    assert "continue" in bloque, (
        "el probe del dial tiene que ignorar un head que no contesta")
    assert "dial_alto" in bloque


def test_the_dial_class_alerts_and_insists_without_failing_the_job():
    """`abliterado` no es `caido`: LiteLLM sirve, y eso es justo el problema.

    - tiene que AVISAR (no puede quedarse en el log)
    - tiene que INSISTIR como `caido`: el fallo es que pasen horas sin enterarse, y
      aqui no hay hora de reset que esperar como con la cuota
    - NO puede hacer fallar el Job: dejaria la Application en Degraded cada 10 min
      y taparia un Degraded de verdad
    """
    src = _watchdog_script()
    assert 'clase = "abliterado"' in src
    assert 'clase in ("caido", "abliterado")' in src, "tiene que insistir como caido"
    assert 'SALIDA = 1 if clase == "caido" else 0' in src, (
        "abliterado no puede hacer fallar el Job")
    # El aviso tiene que decir como devolverlo a 0 y ofrecer la alternativa buena.
    # Se busca la RUTA y el valor por separado: el JSON del ejemplo va escapado
    # dentro de un literal de Python dentro de YAML, y asertar la cadena completa
    # se rompe con cualquier reescape sin que el aviso haya cambiado.
    assert "tooling-uncensored" in src
    assert "/admin/refusal_lambda" in src
    assert "lambda" in src and ": 0" in src
