"""El catalogo de chat.e-dani.com son CUATRO perfiles, y no se puede corromper.

OWU-50 (21-09-2026). Open WebUI deja de inventarse el catalogo: expone los alias
que declara ESTE manifest. Con `model_ids` en la conexion de chat, lo que ve Dani
es literalmente lo que hay aqui, asi que este fichero es la unica red que queda
entre un edicion despistada del manifiesto y el selector del chat.

Que se comprueba y por que cada una:

- Los cuatro existen como entrada publicada del `model_list`. Un alias que solo
  vive en `model_group_alias` NO sale en `/v1/models` (medido el 20-09 con el
  puente de Alibaba): Open WebUI no lo veria y el picker quedaria en menos.
- Cada uno tiene el nivel de pensamiento que le toca. Que los dos `-think`
  piensen distinto es peor que no tenerlos: mismo nombre, distinta conducta.
- Los dos censurados conservan la red de nube, y los dos abliterados NO la
  tienen. Un abliterado con fallback contesta censurado a quien pidio lo
  contrario, con HTTP 200 y sin aviso (#101).
- Los dos abliterados siguen detras de la puerta de keys.

Verificado por mutacion, no por lectura: borrar uno de los cuatro del manifest
tiene que romper CI (`python3 -m pytest tests -k chat_catalog`).
"""
import ast
import pathlib
import types

import pytest
import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# La matriz 2x2 (capacidad x pensar), con el tier que cada nombre debe terminar
# pidiendo. Renombrada el 22-09-2026: los nombres se leen solos ahora.
PERFILES = {
    "qwen38-flash-next": "low",
    "qwen38-flash-next-uncensored": "low",
    "qwen38-off": "off",
    "qwen38-u-off": "off",
}
# Los dos unicos que el hook REESCRIBE (los nombres del residente no necesitan
# reescritura: ya son el residente).
REESCRITOS = ("qwen38-off", "qwen38-u-off")
CENSURADOS = ("qwen38-off",)
ABLITERADOS = ("qwen38-u-off",)
# Los cuatro nombres viejos: retirados del model_list, vivos solo como puente.
PUENTE_VIEJO = ("q38-flash", "q38-flash-think", "q38-flash-u", "q38-flash-u-think")
PUENTE = {
    "q38-flash": "qwen38-off",
    "q38-flash-u": "qwen38-u-off",
    "q38-flash-think": "qwen38-flash-next",
    "q38-flash-u-think": "qwen38-flash-next-uncensored",
}


@pytest.fixture(scope="module")
def config():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if (
            doc
            and doc.get("kind") == "ConfigMap"
            and doc["metadata"]["name"] == "litellm-config"
        ):
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


# Las constantes del hook que deciden que ve cada perfil. Se recorta el hook con
# el mismo procedimiento que `test_uncensored_alias_contract`: AST y exec, sin
# importar litellm. Si falta una, el test ruge en vez de pasar con el modulo a
# medio montar.
WANT_CONST = {
    "TOOLING_PROFILE_ALIASES", "TOOLING_UNCENSORED_ALIASES",
    "TOOLING_MODE_TARGETS",
    "TOOLING_UNCENSORED_MODE_TARGETS", "UNCENSORED_GATED_ALIASES",
    "CAPABILITY_CHAINS", "TOOLING_FALLBACKS",
    "THINKING_TIERS", "SWAPPABLE_ALIASES", "CHAT_PROFILE_RENAMES",
}


@pytest.fixture(scope="module")
def hook():
    src = next(
        d["data"]["litellm_strip_params.py"]
        for d in yaml.safe_load_all(MANIFEST.read_text())
        if d and d.get("kind") == "ConfigMap"
        and d["metadata"]["name"] == "litellm-config"
    )
    keep = [
        n for n in ast.parse(src).body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") in WANT_CONST for t in n.targets)
    ]
    faltan = WANT_CONST - {
        t.id for n in keep for t in n.targets if getattr(t, "id", "") in WANT_CONST
    }
    assert not faltan, f"el hook ya no define: {sorted(faltan)}"
    mod = types.ModuleType("hookcatalog")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


def test_los_cuatro_perfiles_estan_publicados_en_el_model_list(config):
    """Publicados de verdad: un alias de `model_group_alias` no sale en /v1/models."""
    publicados = {e["model_name"] for e in config["model_list"]}
    faltan = sorted(set(PERFILES) - publicados)
    assert not faltan, f"Open WebUI dejaria de ofrecer {faltan}: {sorted(PERFILES)}"
    puente = (config.get("router_settings") or {}).get("model_group_alias") or {}
    colgados = sorted(v for v in PUENTE_VIEJO if puente.get(v) != PUENTE[v])
    assert not colgados, f"nombres viejos sin puente correcto (404 para sesiones fijadas): {colgados}"
    reaparecen = sorted(v for v in PUENTE_VIEJO if v in publicados)
    assert not reaparecen, f"un nombre retirado volvio al model_list: {reaparecen}"


def test_cada_perfile_mantiene_su_nivel_de_pensamiento(hook):
    """Los `-off` sin pensar y los que piensan en `low`.

    Sin entrada en THINKING_TIERS no hay "sin pensar": hay default del SERVIDOR, y
    el chat template de Qwen3.8-Flash-Next arranca en xhigh (medido 31-08: content
    VACIO con finish=length). Por eso este test exige la entrada, no su ausencia.
    """
    for alias, esperado in PERFILES.items():
        assert hook.THINKING_TIERS.get(alias) == esperado, (
            f"{alias} pide {esperado!r}, el manifest le da "
            f"{hook.THINKING_TIERS.get(alias)!r}"
        )


def test_los_dos_que_piensan_terminan_en_el_mismo_nivel(hook):
    """Simetria: censurado y abliterado que prometen pensar, piensan LO MISMO.

    Si la ruta abliterada no honra el nivel elegido, este test no lo puede saber —
    lo dice la medicion contra el motor. Lo que si pilla es que alguien ponga
    `low` en uno y `medium` en el otro y se llamen igual, o que saque el
    abliterado de la tabla (fuera de ella manda el xhigh del servidor: el "mudo").
    """
    assert hook.THINKING_TIERS["qwen38-flash-next"] == hook.THINKING_TIERS["qwen38-flash-next-uncensored"]


@pytest.fixture(scope="module")
def fallbacks(config):
    """`router_settings.fallbacks` aplastado en {modelo: [a donde cae]}.

    Ahi vive el fallback de verdad, no en `TOOLING_FALLBACKS` del hook — esa cadena
    esta en `()` desde que la red se declaro en el router. Y la clave es el nombre
    DIRECTO del residente, porque el hook reescribe el alias ANTES de enrutar: pedir
    `q38-flash` acaba pidiendo `qwen38-flash-next`, y esa es la clave que mira
    LiteLLM. Eso es lo que hace que los cuatro perfiles hereden la red (o se queden
    sin ella) sin declararla ellos.
    """
    tabla = {}
    for paso in config.get("router_settings", {}).get("fallbacks") or []:
        for desde, hacia in (paso or {}).items():
            tabla[desde] = list(hacia or [])
    return tabla


def test_los_censurados_llegan_a_la_red_de_nube(hook, fallbacks):
    """C7 de OWU-50: con el residente saturado el chat contesta (cae en Alibaba).

    No se mira `CAPABILITY_CHAINS` — ahi la red esta vacia a proposito. Se comprueba
    la cadena que decide de verdad: alias censurado -> residente directo del perfil
    -> entrada con destino en `router_settings.fallbacks`. Si alguien quita la red
    del residente, esto rompe.
    """
    for alias in CENSURADOS:
        assert alias in hook.CAPABILITY_CHAINS, alias
    destino = hook.TOOLING_MODE_TARGETS["llm-tp"]
    assert fallbacks.get(destino), (
        f"{destino} sin entrada en router_settings.fallbacks: {sorted(fallbacks)}"
    )


def test_los_abliterados_siguen_sin_red(hook, fallbacks):
    """Un abliterado con red contesta censurado a quien pidio lo contrario, con 200
    y sin aviso — por eso #101 los deja sin ella.

    Se comprueba en los dos sitios donde se puede colar: la cadena del hook y la
    tabla del router, por el nombre directo al que reescribe el alias.
    """
    for alias in ABLITERADOS:
        assert hook.CAPABILITY_CHAINS[alias]["fallbacks"] == (), alias
    for destino in hook.TOOLING_UNCENSORED_MODE_TARGETS.values():
        assert not fallbacks.get(destino), f"{destino} tiene red y no deberia"


def test_los_abliterados_estan_detras_de_la_puerta_de_keys(hook):
    """Un nombre uncensored fuera del gate no pasa por el control: es abrir la
    ablacion a cualquier key por la puerta de atras."""
    for alias in ABLITERADOS:
        assert alias in hook.TOOLING_UNCENSORED_ALIASES, alias
    # el nombre directo del residente abliterado entra al gate por ser destino de
    # TOOLING_UNCENSORED_MODE_TARGETS, no por estar en el conjunto de perfiles
    assert "qwen38-flash-next-uncensored" in hook.UNCENSORED_GATED_ALIASES
    for alias in ABLITERADOS:
        assert alias in hook.UNCENSORED_GATED_ALIASES, alias


def test_el_puente_del_hook_y_el_del_router_coinciden(config, hook):
    """Los DOS puentes del renombrado tienen que decir lo mismo.

    `model_group_alias` elige el deployment, pero el hook ve el nombre CRUDO
    pedido y sus tablas (CAPABILITY_CHAINS, THINKING_TIERS, el gate uncensored)
    lo miran a el. Si alguien toca uno y no el otro, el trafico puenteado sale
    al pool con `model: "tooling"` y vLLM lo rechaza con 404 — y de paso mete el
    deployment en cooldown (medido 22-09).
    """
    puente = (config.get("router_settings") or {}).get("model_group_alias") or {}
    viejos = {k: v for k, v in puente.items() if k.startswith("q38-flash")}
    assert viejos == hook.CHAT_PROFILE_RENAMES, (
        f"puente del router {viejos} != puente del hook {hook.CHAT_PROFILE_RENAMES}"
    )
    publicados = {e["model_name"] for e in config["model_list"]}
    colgados = {k: v for k, v in viejos.items() if v not in publicados}
    assert not colgados, f"puentes apuntando a un grupo no publicado: {colgados}"


def test_los_reescritos_son_reescritos_al_residente_vivo(hook):
    """Sin entrada en CAPABILITY_CHAINS la reescritura esta MUERTA: sale al pool
    sin `cache_salt` y contesta el residente equivocado con HTTP 200."""
    for alias in REESCRITOS:
        assert alias in hook.CAPABILITY_CHAINS, alias
    for alias in CENSURADOS:
        assert alias in hook.TOOLING_PROFILE_ALIASES, alias
