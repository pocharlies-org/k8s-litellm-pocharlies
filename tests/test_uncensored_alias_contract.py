"""Contrato de los alias abliterados, contra el mecanismo QUE CORRE.

POR QUE SE REESCRIBIO ESTE FICHERO (19-08-2026). La version anterior extraia
`extra_litellm_params` del ConfigMap de `litellm-dgx-backend-sync` y la ejecutaba.
Ese controlador esta a **0 replicas declaradas en git** desde be9e2b1: los alias
ya no se registran por HTTP contra /model/new, se declaran en el `model_list`
estatico con `store_model_in_db: false`. O sea que el test pasaba en verde
validando codigo que nada ejecuta, y el mismo error se repitio al anadir el alias
de capacidad: el cambio fue a `sync.py` y no habria registrado nada.

Asi que aqui NO se toca el sync. Se comprueban las dos piezas vivas:

  1. el `model_list` estatico — donde vive el sello `cache_salt` de cada backend
  2. el hook `litellm_strip_params.py` — quien reescribe el alias de CAPACIDAD al
     nombre directo del residente vivo

Las dos lambdas son medidas y NO intercambiables: DeepSeek 1.5 (a 1.0 aun rechaza
5-7/10), Qwen3.8 1.0 (a 1.5 pierde 26,8 puntos de MMLU-Pro, p=0,0000).
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

CAPABILITY = "tooling-uncensored"

# Alias censurados que tienen que llevar sello EXPLICITO a 0. Desde que el
# residente sirve con el lambda global en 1.0, "sin sello" ya no significa
# "censurado": significa "usa el global", que es ablado.
CENSURABLES = ("qwen38-flash-next", "tooling")
# 01-09-2026: se va `deepseek-v4-flash-0731-uncensored` (refusal:1.5) con la
# retirada de DeepSeek.
# 01-09 (tarde): entra `qwen38-flash-next-uncensored` con su propio sello. No es
# el 1.5 de DeepSeek heredado: 1.5 es lo que sale de medir por diff de shards
# windowsxp811203/Qwen3.8-Flash-Next-Abliterated (lambda_eff 1.4994-1.5008,
# spread 0.09%) y aplicar el criterio del 27B, donde la variacion entre capas se
# absorbe en un coef y aqui ese coef sale 1. Coincidir con DeepSeek es coincidencia.
MODEL_SCOPED_LAMBDA = {
    "qwen38-27b-uncensored": "refusal:1.0",
    "qwen38-flash-next-uncensored": "refusal:1.0",
}

WANT_FN = {"_component_is_ready", "_ready_tooling_modes",
           "_select_ready_tooling_mode", "_tooling_mode_for_model",
           "_compute_mode_allows_local", "_tooling_uncensored_target",
           "_deployment_cache_salt", "_preserve_uncensored_seal"}
WANT_CONST = {"TOOLING_UNCENSORED_MODE_TARGETS", "TOOLING_UNCENSORED_ALIASES",
              "ABLITERATED_HEALTH_URLS",
              "CAPABILITY_CHAINS", "TOOLING_FALLBACKS",
              "TOOLING_MODE_TARGETS", "TOOLING_MODE_COMPONENTS",
              "TOOLING_PROFILE_ALIASES",
              "CAPABILITY_CHAINS", "TOOLING_FALLBACKS"}


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


@pytest.fixture(scope="module")
def model_list():
    for doc in _docs():
        data = doc.get("data") or {}
        if doc.get("kind") == "ConfigMap" and "config.yaml" in data:
            cfg = yaml.safe_load(data["config.yaml"])
            return {m["model_name"]: m for m in cfg["model_list"]}
    raise AssertionError("no encuentro el config.yaml de LiteLLM")


@pytest.fixture(scope="module")
def hook():
    """El hook REAL, solo sus funciones puras. Sin importar litellm."""
    src = next(
        d["data"]["litellm_strip_params.py"]
        for d in _docs()
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )
    tree = ast.parse(src)
    keep = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets))
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("hookpure")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


# ── 1. el model_list estatico: donde vive el sello ──────────────────────────────

def test_each_model_scoped_alias_carries_ITS_OWN_measured_lambda(model_list):
    for name, salt in MODEL_SCOPED_LAMBDA.items():
        entry = model_list[name]
        assert entry["litellm_params"]["extra_body"] == {"cache_salt": salt}, name


def test_the_capability_alias_carries_NO_salt_of_its_own(model_list):
    """Y no es un olvido.

    `tooling-uncensored` apunta al Service de pool, que no sabe quien contesta.
    Un `cache_salt` fijo aqui seria el valor equivocado para uno de los dos
    residentes SIEMPRE: 1.0 deja a DeepSeek rechazando, 1.5 le cuesta a Qwen 26,8
    puntos de MMLU-Pro. El sello viaja en la entrada del nombre directo al que el
    hook reescribe, que es la unica que sabe de que backend habla.

    03-09-2026: el ancla `tooling_pool_params` ahora SI lleva sello (refusal:0),
    y por eso `tooling-uncensored` lo hereda en el YAML. Eso no rompe el diseño:
    el uncensored no se sirve de esta entrada. El hook reescribe el alias al
    nombre de destino (TOOLING_UNCENSORED_MODE_TARGETS ->
    qwen38-flash-next-uncensored / qwen38-27b-uncensored), y cada destino lleva
    su sello en su propia entrada. Se comprueba el destino, no el alias.
    """
    entry = model_list[CAPABILITY]
    heredado = (entry["litellm_params"].get("extra_body") or {}).get("cache_salt")
    assert heredado in (None, "refusal:0"), entry["litellm_params"]


def test_the_uncensored_DESTINATIONS_carry_the_seal(model_list):
    """Donde viaja de verdad el sello uncensored: los nombres de destino."""
    for destino in ("qwen38-flash-next-uncensored", "qwen38-27b-uncensored"):
        body = model_list[destino]["litellm_params"].get("extra_body") or {}
        assert body.get("cache_salt") == "refusal:1.0", (destino, body)


def test_the_censored_aliases_carry_an_EXPLICIT_zero(model_list):
    """Un alias censurado NO puede depender del lambda global.

    03-09-2026: `qwen38-flash-next` no llevaba sello y, con el global en 1.0
    desde #38, salia ablado igual que su gemelo `-uncensored`. El alias
    "censurado" no censuraba nada y ningun test lo cazaba, porque el test que
    habia decia justamente lo contrario: "no tiene que ganar un sello".

    Con el sello a 0 el alias es censurado pase lo que pase con el global.
    """
    for alias in CENSURABLES:
        body = model_list[alias]["litellm_params"].get("extra_body") or {}
        assert body.get("cache_salt") == "refusal:0", (alias, body)


def test_no_uncensored_alias_has_a_fallback_to_the_cloud():
    """Un fallback aqui contestaria una peticion abliterada desde un modelo de
    nube CON sus barreras puestas, HTTP 200 y sin un aviso."""
    text = MANIFEST.read_text()
    for alias in (CAPABILITY, *MODEL_SCOPED_LAMBDA):
        assert f"- {alias}: [" not in text, alias


# ── 2. el hook: quien reescribe la capacidad al residente vivo ──────────────────

def test_the_capability_alias_resolves_to_the_ABLITERATED_resident(hook):
    """Al abliterado del perfil, no al residente normal: si resolviera al
    residente censurado se perderia el sello y serviria censurado.

    01-09-2026: `llm-tp` estuvo SIN abliterado al retirarse DeepSeek, porque no
    existia port rank-1 de la arquitectura. Ya existe: el anclaje de
    k8s-ai-pocharlies@claude/qwen38-flash-next-vllm-rank1-20260901, sobre el
    modulo vendorizado que el registry realmente instancia. Lo que este test fija
    sigue siendo lo mismo y no se negocia: que el alias resuelva al nombre
    ABLITERADO del perfil, y que la ausencia de destino se resuelva a un fallo
    VISIBLE -- nunca a un residente censurado en silencio.

    OJO a lo que este test NO puede probar: que el backend parse el sello. Eso se
    comprueba contra el pod (GET /admin/refusal_lambda != 404), no aqui.
    """
    assert hook.TOOLING_UNCENSORED_ALIASES == frozenset({CAPABILITY})
    components = {
        "llm-tp": {
            # Las keys son HISTORICAS a proposito: son el contrato de
            # componentes de compute_mode.COMPONENTS del dashboard y hoy
            # apuntan a los deploys qwen38-flash-next-*.
            "dgx1": [{"name": "deepseek-worker", "ready": True,
                      "desired_replicas": 1, "ready_replicas": 1}],
            "dgx2": [{"name": "deepseek-head", "ready": True,
                      "desired_replicas": 1, "ready_replicas": 1}],
        },
        "creative": {
            "dgx1": [{"name": "dense-uncensored", "ready": True,
                      "desired_replicas": 1, "ready_replicas": 1}],
        },
    }
    for mode, want in (("llm-tp", ("qwen38-flash-next-uncensored", None)),
                       ("creative", ("qwen38-27b-uncensored", None))):
        got = hook._tooling_uncensored_target(
            {"effective_mode": mode, "desired_mode": mode, "phase": "ready",
             "components": components[mode]}
        )
        assert got == want, (mode, got)
    # El cambio deseado puede estar pendiente: manda el residente que siga Ready.
    assert hook._tooling_uncensored_target({
        "effective_mode": "creative",
        "desired_mode": "llm-tp",
        "phase": "waiting",
        "components": components["creative"],
    }) == ("qwen38-27b-uncensored", None)
    # Y cada destino es una entrada con sello propio, no un nombre inventado.
    assert set(hook.TOOLING_UNCENSORED_MODE_TARGETS.values()) == set(MODEL_SCOPED_LAMBDA)


def test_the_capability_alias_is_in_CAPABILITY_CHAINS_or_the_rewrite_never_runs(hook):
    """El fallo que costo tres intentos, y el unico que se caza midiendo.

    `cap_alias` solo se fija si el modelo pedido es una clave de
    CAPABILITY_CHAINS. Con el alias declarado en `model_list` y en
    TOOLING_UNCENSORED_ALIASES pero fuera de ese dict, la rama de reescritura
    queda MUERTA: la peticion sale al Service de pool sin `cache_salt`, devuelve
    200 y sirve el residente CENSURADO bajo un nombre que dice lo contrario.
    Medido: 3/3 rechazos identicos a `tooling`.

    Y sus fallbacks tienen que estar VACIOS: las cuentas Luna no pueden llevar el
    sello, asi que no puede haber cadena que recorrer.
    """
    for alias in hook.TOOLING_UNCENSORED_ALIASES:
        assert alias in hook.CAPABILITY_CHAINS, alias
        assert hook.CAPABILITY_CHAINS[alias]["fallbacks"] == (), alias


def test_the_two_target_maps_stay_disjoint(hook):
    """El mapa censurado y el abliterado no pueden compartir destino: seria
    servir uno como el otro."""
    assert not (set(hook.TOOLING_MODE_TARGETS.values())
                & set(hook.TOOLING_UNCENSORED_MODE_TARGETS.values()))
    assert not (hook.TOOLING_PROFILE_ALIASES & hook.TOOLING_UNCENSORED_ALIASES)


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in _docs()
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


def _function_body(src, name):
    """Texto de UNA funcion del hook. Para asertar sobre la rama que NO existe."""
    start = src.index(name)
    body = src[start:]
    cut = min(
        (body.index(m) for m in ("\n    def ", "\n    async def ", "\n    # ──")
         if m in body[1:]),
        default=len(body),
    )
    return body[:cut]


def _code_only(text):
    """Igual que arriba pero SIN comentarios ni docstrings.

    Hace falta porque estas funciones explican en prosa justo lo que NO deben
    hacer ("no se usa `_alias_has_deployments` aqui, y es el arreglo del 19-08"),
    y un assert sobre el texto crudo se dispararia con la explicacion. Un test que
    falla por un comentario no mide nada.
    """
    out, in_doc = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            # docstring de una linea vs bloque
            if not (len(stripped) > 5 and stripped.endswith(stripped[:3])):
                in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        out.append(line.split("  #")[0])
    return "\n".join(out)


def test_the_uncensored_resolver_has_no_fallback_net_at_all():
    """El resolver abliterado nunca puede degradar a una ruta censurada.

    Prefiere 503. Se comprueba sobre el texto de la función porque la rama que
    importa es la que NO existe.
    """
    body = _code_only(_function_body(_hook_source(), "async def _resolve_tooling_uncensored"))
    assert "TOOLING_FALLBACKS" not in body
    assert "503" in body


def test_liveness_is_by_HEALTH_not_by_registration(hook):
    """El cuarto no-op, el unico que la auditoria del 19-08 encontro sin cobertura.

    `_alias_has_deployments` pregunta si el alias esta REGISTRADO en el router y
    falla ABIERTO. Con `store_model_in_db: false` el registro sale de git, asi que
    un alias declarado responde "si" aunque su Deployment este a 0 replicas.
    Medido: `qwen38-27b-uncensored` seguia publicado en /v1/models con backend 0/0 y
    devolvia **HTTP 500** (`Connection error. No fallback model group`), no el 503
    con Retry-After que el docstring prometia: la rama del 503 era inalcanzable.

    La ruta abliterada no admite ningun fallback a proposito, asi que su unica
    alternativa a un 500 es preguntar por SALUD.
    """
    body = _code_only(_function_body(_hook_source(), "async def _resolve_tooling_uncensored"))

    assert "_alias_has_deployments" not in body, (
        "el resolver abliterado volvio a fiarse del REGISTRO; con model_list "
        "estatico eso es siempre True y la rama del 503 queda inalcanzable")
    assert "_abliterated_target_ready" in body

    # Todo destino del mapa necesita URL de health, o no se puede decir nada de el.
    assert set(hook.TOOLING_UNCENSORED_MODE_TARGETS.values()) <= set(
        hook.ABLITERATED_HEALTH_URLS), (
        "hay un destino abliterado sin URL de health declarada: se serviria a ciegas")


def test_the_health_probe_fails_CLOSED():
    """Al contrario que el resto del hook, que falla abierto a proposito.

    Si no se puede confirmar que el backend abliterado esta arriba: 503. Un 503 es
    reintentable y dice la verdad; un 500 no, y degradar a otro sitio seria servir
    censurado a quien pidio lo contrario. Un destino sin URL declarada tambien
    cuenta como "no listo", que es lo que caza el descuido de manana.
    """
    body = _code_only(_function_body(_hook_source(), "async def _abliterated_target_ready"))
    assert "abliterated_health_url_missing" in body
    assert "ready = False" in body
    assert "return True" not in body, (
        "un `return True` suelto en el probe lo convierte en fail-open")


def test_cache_salt_is_not_stripped_by_the_family_sampling_hook():
    """FAMILY_SAMPLING borra claves de sampling por familia. Si `cache_salt`
    entrara en un `drop`, el alias moriria en el hook en vez de en el registro,
    que es mucho mas dificil de ver."""
    text = MANIFEST.read_text()
    families = text[text.index("FAMILY_SAMPLING = {"):]
    families = families[: families.index("\n    }\n") + 7]
    assert "cache_salt" not in families


# ── 3. el guard que se me escapo ────────────────────────────────────────────────

def test_the_retired_sync_controller_stays_at_zero_replicas():
    """Este test existe porque el alias de capacidad se anadio primero a `sync.py`
    y no habria registrado NADA.

    `litellm-dgx-backend-sync` se conservo a 0 replicas en be9e2b1 (la Application
    lleva prune: false), asi que su `sync.py` sigue en el repo, se sigue leyendo
    como si fuera el sitio donde viven los alias, y no ejecuta una linea. Si algun
    dia vuelve a 1, este test se pone rojo y toca decidir a proposito quien es la
    fuente de la verdad — no descubrirlo por un alias que no aparece.
    """
    for doc in _docs():
        if (doc.get("kind") == "Deployment"
                and doc["metadata"]["name"] == "litellm-dgx-backend-sync"):
            assert doc["spec"].get("replicas") == 0, (
                "el controlador de alias volvio a estar arriba: hay dos fuentes de "
                "verdad para el model_list"
            )
            return
    raise AssertionError("ya no existe el Deployment litellm-dgx-backend-sync")


# ── 3. el sello sobrevive a un extra_body ajeno ─────────────────────────────────
#
# extra_body es un DICT y el de la peticion SUSTITUYE al de la deployment, no se
# fusiona con el. Asi que cualquiera que mande extra_body -- incluido este mismo
# hook al fijar el nivel de pensamiento -- borraba el `cache_salt` del alias
# abliterado, que entonces contestaba EXACTAMENTE igual que el censurado sin dar
# un solo error. Medido el 22-08-2026 contra el residente vivo (sha1, temp 0):
#
#   qwen38-27b                             61888e39  rechaza
#   qwen38-27b-uncensored                  488bb2a3  contesta
#   qwen38-27b-uncensored + extra_body     61888e39  rechaza   <- sello borrado
#   qwen38-27b-uncensored + effort=none    61888e39  rechaza   <- lo borro el hook
#
# El ultimo es el grave: `reasoning_effort` es AMBIENTE en los clientes agente.

class _Log:
    def __init__(self): self.lines = []
    def info(self, *a): self.lines.append(a)
    debug = info
    warning = info


@pytest.fixture
def sealed(hook, monkeypatch):
    """El hook con logs de mentira y una deployment que declara su sello."""
    monkeypatch.setattr(hook, "log", _Log(), raising=False)
    monkeypatch.setattr(hook, "sampling_log", _Log(), raising=False)
    monkeypatch.setattr(hook, "_deployment_cache_salt",
                        lambda alias: "refusal:1.0" if alias.endswith("-uncensored") else None,
                        raising=False)
    return hook


def test_an_alien_extra_body_does_NOT_strip_the_seal(sealed):
    data = {"model": "qwen38-27b-uncensored",
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    sealed._preserve_uncensored_seal(data)
    assert data["extra_body"]["cache_salt"] == "refusal:1.0"
    # y no se lleva por delante lo que ya habia
    assert data["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_an_explicit_client_salt_WINS_over_the_deployment(sealed):
    # Es como el playground barre lambdas sobre el alias base: quitarselo seria
    # cambiarle el lambda a quien lo pidio a mano.
    data = {"model": "qwen38-27b-uncensored",
            "extra_body": {"cache_salt": "refusal:2.0"}}
    sealed._preserve_uncensored_seal(data)
    assert data["extra_body"]["cache_salt"] == "refusal:2.0"


def test_a_censored_alias_never_gains_a_seal(sealed):
    data = {"model": "qwen38-27b", "extra_body": {"chat_template_kwargs": {}}}
    sealed._preserve_uncensored_seal(data)
    assert "cache_salt" not in data["extra_body"]


def test_without_extra_body_nothing_is_created(sealed):
    # Sin extra_body propio, el de la deployment llega intacto por si solo: crear
    # uno aqui seria trabajo de mas y una via nueva por la que equivocarse.
    data = {"model": "qwen38-27b-uncensored"}
    sealed._preserve_uncensored_seal(data)
    assert "extra_body" not in data


def test_it_never_raises_on_a_broken_request(sealed):
    for data in ({"extra_body": {"a": 1}}, {"model": None, "extra_body": {}},
                 {"model": "x", "extra_body": "no soy un dict"}):
        sealed._preserve_uncensored_seal(data)   # no debe lanzar
