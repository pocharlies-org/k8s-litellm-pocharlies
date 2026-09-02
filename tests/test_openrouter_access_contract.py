"""Quien puede pedir el catalogo de OpenRouter, y por que el gate va por PREFIJO.

POR QUE EXISTE ESTE FICHERO. Los alias `or-*` son el primer upstream del gateway
que sale a Internet, y ademas apuntan a modelos `:free`, que son gratis justamente
porque el proveedor se queda el dato. Lo que decide quien los alcanza NO es el
allowlist de la key: `models: []` significa TODOS los modelos y 7 de las 10 keys
vivas lo tienen asi, de modo que en el instante en que un alias entra en el
model_list lo heredan ocho keys solas — incluida la de `document-intake`, que
procesa facturas. La unica capa que puede cerrarlo es el hook.

Y va por PREFIJO, no por lista literal, porque el bloque `or-*` esta pensado para
crecer, y CRECE A MANO: se edita a pelo en `k8s/manifest.yaml`, en un commit, y puede
crecer SIN pasar por este test. (Durante un tiempo este fichero afirmaba que lo
alimentaba un panel en `openrouter.e-dani.com` que inserta entradas por PR. No existe
tal panel: 404 en todo el dominio y sin repo. La conclusion — gate por prefijo — era
la correcta; el razonamiento era falso, y ya costo una investigacion encima, commit
603cc3f.) Con una lista literal, cada alias nuevo naceria ABIERTO hasta que alguien se
acordara de venir a cerrarlo; con prefijo nace cerrado por construccion, que es justo
lo que compensa que crezca a mano. El precedente esta medido: `gpt-5.6-luna` volvio al
model_list el 25-08 y `sauvage-shield` lo heredo sola porque lo llevaba escrito de
antes de la purga.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"

WANT_FN = {"_openrouter_access_denied"}
WANT_CONST = {"OPENROUTER_ALIAS_PREFIX", "OPENROUTER_ALLOWED_KEY_ALIASES"}

ALLOWED_KEY = "opencode-20260630-local"


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _config():
    cm = next(
        d for d in _docs()
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    return cm, yaml.safe_load(cm["data"]["config.yaml"])


def _deployment_env():
    dep = next(
        d for d in _docs()
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm"
    )
    container = next(
        c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "litellm"
    )
    return {e["name"]: e for e in container["env"]}


def _gate(env):
    """Reejecuta las constantes del hook con un os.environ controlado.

    Hace falta porque OPENROUTER_ALLOWED_KEY_ALIASES se calcula EN IMPORT desde
    os.environ: sin volver a ejecutar el modulo no se puede observar el caso
    fail-closed.
    """
    cm, _ = _config()
    tree = ast.parse(cm["data"]["litellm_strip_params.py"])
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("openroutergate")
    fake_os = types.SimpleNamespace(environ=dict(env))
    mod.os = fake_os
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


@pytest.fixture(scope="module")
def open_gate():
    return _gate({"LITELLM_OPENROUTER_ALLOWED_KEYS": ALLOWED_KEY})


def test_a_deployment_without_the_variable_denies_everyone():
    """Fail-CLOSED, al reves que el gate abliterado.

    Si la variable llega vacia, el fallo seguro es que los alias queden
    inservibles. El peligroso seria que quedaran abiertos.
    """
    gate = _gate({})
    for key in (ALLOWED_KEY, "master", "synapse", "document-intake"):
        denied, detail = gate._openrouter_access_denied("or-glm", key)
        assert denied, f"{key} deberia estar denegada con la lista vacia"
        assert detail["allowed"] == []


def test_the_allowed_key_gets_through_and_nobody_else_does(open_gate):
    denied, _ = open_gate._openrouter_access_denied("or-glm", ALLOWED_KEY)
    assert not denied

    for key in ("master", "synapse", "document-intake", "sauvage-shield", "codex"):
        denied, detail = open_gate._openrouter_access_denied("or-glm", key)
        assert denied, f"{key} no deberia alcanzar OpenRouter"
        assert detail["requested"] == "or-glm"


def test_an_alias_that_does_not_exist_yet_is_already_closed(open_gate):
    """El motivo de usar prefijo: un alias futuro nace cerrado, sin tocar el gate."""
    denied, _ = open_gate._openrouter_access_denied("or-un-modelo-que-aun-no-existe", "synapse")
    assert denied


def test_the_gate_ignores_everything_that_is_not_an_openrouter_alias(open_gate):
    for model in ("tooling", "deepseek-v4-flash-0731", "gpt-5.6-sol", "qwen35-4b", "", None):
        denied, _ = open_gate._openrouter_access_denied(model, "synapse")
        assert not denied, f"{model!r} no es un alias de OpenRouter y no le toca a este gate"


def test_the_deployment_ships_a_non_empty_allowlist():
    env = _deployment_env()
    value = env["LITELLM_OPENROUTER_ALLOWED_KEYS"]["value"]
    assert value.strip(), "lista vacia = alias inservibles (ver el test de fail-closed)"
    assert ALLOWED_KEY in [item.strip() for item in value.split(",")]


def test_the_api_key_comes_from_the_external_secret_not_from_a_literal():
    env = _deployment_env()
    ref = env["OPENROUTER_API_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "litellm-secrets", "key": "OPENROUTER_API_KEY"}

    es = next(
        d for d in _docs()
        if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == "litellm-secrets"
    )
    entry = next(e for e in es["spec"]["data"] if e["secretKey"] == "OPENROUTER_API_KEY")
    assert entry["remoteRef"] == {"key": "secret/litellm", "property": "OPENROUTER_API_KEY"}


def _openrouter_entries():
    _, cfg = _config()
    return [m for m in cfg["model_list"] if str(m["model_name"]).startswith("or-")]


def test_every_openrouter_alias_declares_what_opencode_needs():
    """`max_output_tokens` no es cosmetico.

    El plugin de opencode rechaza la config ENTERA con
    `Missing key ...["limit"]["output"]` si un modelo llega sin el, asi que un alias
    mal declarado no se rompe solo a si mismo: deja al usuario sin catalogo.
    """
    entries = _openrouter_entries()
    assert entries, "el bloque or-* ha desaparecido del model_list"
    for m in entries:
        info = m["model_info"]
        assert info["max_output_tokens"] > 0, m["model_name"]
        assert info["max_input_tokens"] > 0, m["model_name"]
        assert info["backend"] == "openrouter-free", m["model_name"]


def test_no_background_health_check_burns_the_daily_quota():
    """4 replicas x 96 llamadas/dia contra una cuota diaria se la comen entera."""
    for m in _openrouter_entries():
        assert m["model_info"]["disable_background_health_check"] is True, m["model_name"]


def test_every_openrouter_alias_carries_a_one_line_label():
    """Cada alias `or-*` lleva `model_info.description` de UNA linea.

    El picker de opencode no tiene donde decir que `or-ultra` es un frontier de 550B
    y `or-lfm` un nano de 2,6B que el propio fabricante desaconseja para coding. La
    etiqueta es lo unico que lo distingue en el menu, y sale del campo `description`
    de `GET https://openrouter.ai/api/v1/models`.

    Va de una linea porque el picker la pinta en un `span` sin salto de linea: un
    salto aqui no parte la etiqueta, parte el YAML del ConfigMap.
    """
    for m in _openrouter_entries():
        description = m["model_info"].get("description")
        assert isinstance(description, str) and description.strip(), (
            f"{m['model_name']} sin model_info.description"
        )
        assert "\n" not in description, m["model_name"]
        assert len(description) <= 120, (m["model_name"], len(description))


def test_openrouter_is_never_the_silent_destination_of_a_degradation():
    """Mismo criterio que el puente ChatGPT: si el residente local cae, falla visible.

    Un alias de nube dentro de CAPABILITY_CHAINS entra ademas en VISION_DIVERTIBLE,
    que es `frozenset(CAPABILITY_CHAINS)`.
    """
    cm, cfg = _config()
    names = {m["model_name"] for m in _openrouter_entries()}

    for name in names:
        assert name not in yaml.dump(cfg.get("router_settings", {})), \
            f"{name} aparece en router_settings"

    source = cm["data"]["litellm_strip_params.py"]
    chains = next(
        n for n in ast.parse(source).body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "CAPABILITY_CHAINS" for t in n.targets)
    )
    declared = {
        k.value for k in ast.walk(chains)
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }
    assert not (names & declared), f"alias de OpenRouter en CAPABILITY_CHAINS: {names & declared}"
