"""Contrato del catalogo de chat: los cuatro perfiles de OWU-50/OWU-51.

`q38-flash` / `q38-flash-think` / `q38-flash-u` / `q38-flash-u-think` son los
cuatro nombres que Open WebUI publica como perfiles del residente (decision de
Dani 20-09: los cuatro para las cuatro cuentas de chat). Su propiedad
diferente respecto a `tooling` es que son entradas PURAS de pool: declaran su
`reasoning_effort` en sus propios `litellm_params` y NO pasan por ninguna tabla
de reescritura del hook. Si alguien los mete en `THINKING_TIERS` o en
`CAPABILITY_CHAINS`, el hook rescribiria el alias al nombre directo del
residente, el router mergearia los params del DESTINO y el esfuerzo declarado
moriria antes del motor — el fallo que el dictamen de arquitectura de OWU-50
(eligio la opcion B) existe para evitar. Este contrato es la verja mecanica de
esa decision: la spec de OWU-51 PROHIBE anadirlos a esas tablas, y aqui se
convierte en test rojo.

Casable con `-k 'chat_catalog or q38_flash'`.
"""
import ast
import os as _os
import types
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

POOL_API_BASE = "http://tooling.llm.svc.cluster.local:8000/v1"

# Los cuatro, en el orden de la spec. effort = lo que DECLARA cada entrada.
CHAT_PROFILES = {
    "q38-flash": "none",
    "q38-flash-think": "low",
    "q38-flash-u": "none",
    "q38-flash-u-think": "low",
}
CENSURADOS = ("q38-flash", "q38-flash-think")
ABLITERADOS = ("q38-flash-u", "q38-flash-u-think")

WANT_FN = {"_uncensored_access_denied"}
WANT_CONST = {
    "UNCENSORED_ALLOWED_KEY_ALIASES",
    "UNCENSORED_GATED_ALIASES",
    "Q38_UNCENSORED_ALIASES",
    "Q38_POOL_ALIASES",
    "TOOLING_UNCENSORED_ALIASES",
    "TOOLING_UNCENSORED_MODE_TARGETS",
    "TOOLING_PROFILE_ALIASES",
    "THINKING_TIERS",
    "CAPABILITY_CHAINS",
    "TOOLING_FALLBACKS",
    "FORWARD_REASONING_MODELS",
    "STRICT_LEADING_SYSTEM_MODELS",
}


def _docs():
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _hook_source():
    return next(
        d["data"]["litellm_strip_params.py"]
        for d in _docs()
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config"
    )


@pytest.fixture(scope="module")
def config():
    for doc in _docs():
        data = doc.get("data") or {}
        if doc.get("kind") == "ConfigMap" and "config.yaml" in data:
            return yaml.safe_load(data["config.yaml"])
    raise AssertionError("no encuentro el config.yaml de LiteLLM")


@pytest.fixture(scope="module")
def model_list(config):
    return {m["model_name"]: m for m in config["model_list"]}


@pytest.fixture(scope="module")
def hook():
    """El hook real, solo sus piezas puras. Sin importar litellm."""
    tree = ast.parse(_hook_source())
    keep = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") in WANT_CONST for t in n.targets)
        )
    ]
    missing = WANT_FN - {n.name for n in keep if isinstance(n, ast.FunctionDef)}
    assert not missing, f"el hook ya no define: {sorted(missing)}"
    mod = types.ModuleType("chat_catalog_hook")
    # `UNCENSORED_ALLOWED_KEY_ALIASES` se construye con os.environ: el modulo
    # recortado no arrastra los imports del hook, asi que se inyecta.
    mod.__dict__["os"] = _os
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


# ── 1. los cuatro existen y apuntan al pool con el ancla heredada ───────────────

@pytest.mark.parametrize("alias", sorted(CHAT_PROFILES))
def test_q38_flash_chat_alias_exists_on_the_pool(model_list, alias):
    entry = model_list[alias]
    params = entry["litellm_params"]
    info = entry["model_info"]
    assert params["api_base"] == POOL_API_BASE, alias
    assert params["model"] == "openai/tooling", alias
    assert info["backend"] == "profile-resident", alias
    # Herencia del ancla, no bloque copiado: los demas campos del ancla siguen
    # ahi (timeout, admision paralela, api_key).
    for key in ("api_key", "timeout", "max_parallel_requests"):
        assert key in params, (alias, key)
    assert info["disable_background_health_check"] is True, alias


def test_chat_catalog_alias_reutiliza_el_ancla_sin_copiar_bloques():
    """El `<<:` sobre el ancla es el contrato de mantenimiento: un bloque copiado
    se separa del ancla en silencio y la proxima edicion del pool no le llega."""
    text = MANIFEST.read_text()
    # Los cuatro nuevos mergean el ancla de params (la definicion del ancla en
    # `tooling` y la referencia seca de `tooling-uncensored` no cuentan).
    assert text.count("<<: *tooling_pool_params") == 4, text.count("<<: *tooling_pool_params")
    # Los dos censurados heredan el model_info entero (mas la referencia seca de
    # `tooling-uncensored`); los dos `-u` lo mergean para reemplazar el menu.
    assert text.count("model_info: *tooling_pool_info") == 3
    assert text.count("<<: *tooling_pool_info") == 2


# ── 2. el effort declarado sobrevive: se declara en la entrada, fuera de las
#      tablas que lo matan ───────────────────────────────────────────────────────

@pytest.mark.parametrize("alias,effort", sorted(CHAT_PROFILES.items()))
def test_q38_flash_alias_declara_su_reasoning_effort_en_entrada(model_list, alias, effort):
    params = model_list[alias]["litellm_params"]
    # Entrecomillado: un `none` pelado en YAML es string, pero un `no` seria
    # booleano — el contrato es que viaje como texto al motor, y ahi no hay
    # ambiguedad que valga.
    assert params.get("reasoning_effort") == effort, (alias, params.get("reasoning_effort"))
    assert isinstance(params["reasoning_effort"], str), alias


@pytest.mark.parametrize("alias", sorted(CHAT_PROFILES))
def test_q38_flash_alias_no_esta_en_las_tablas_que_matan_el_effort(hook, alias):
    """THINKING_TIERS y CAPABILITY_CHAINS son la puerta de la reescritura.

    Entrar ahi = el hook decide el nivel o reescribe el alias, y el valor
    declarado en la entrada del model_list muere antes del motor. La spec de
    OWU-51 lo prohibe; este test es la verja.
    """
    assert alias not in hook.THINKING_TIERS, alias
    assert alias not in hook.CAPABILITY_CHAINS, alias
    assert alias not in hook.TOOLING_PROFILE_ALIASES, alias
    assert alias not in hook.TOOLING_UNCENSORED_ALIASES, alias
    # Y tampoco en SWAPPABLE_ALIASES: seria un no-op (`_family_of_alias` lee
    # `openai/tooling`, sin marca de familia -> return temprano), pero declarar
    # ahi describiria una superficie que no existe — ver el comentario de la
    # tabla en el hook.
    tree = ast.parse(_hook_source())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "SWAPPABLE_ALIASES" for t in node.targets
        ):
            value = node.value
            # La tabla es `frozenset({...})`: un Call, no un literal directo.
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "frozenset":
                value = value.args[0]
            assert alias not in ast.literal_eval(value), alias


# ── 3. los `-u` gateados: en el gate, y 403 para quien no esta en la lista ──────

@pytest.mark.parametrize("alias", ABLITERADOS)
def test_q38_flash_uncensored_alias_esta_en_el_gate(hook, alias):
    assert alias in hook.Q38_UNCENSORED_ALIASES, alias
    assert alias in hook.UNCENSORED_GATED_ALIASES, alias


@pytest.mark.parametrize("alias", ABLITERADOS)
def test_q38_flash_uncensored_deniega_la_key_no_listada(hook, alias):
    denied, detail = hook._uncensored_access_denied(alias, "algun-key-que-nadie-escribio")
    assert denied is True, (alias, detail)
    assert detail["requested"] == alias
    # `open-webui-v3` SI puede (OWU-50: los cuatro perfiles para las cuentas de
    # chat; decision de Dani 20-09).
    assert hook._uncensored_access_denied(alias, "open-webui-v3") == (False, None)


@pytest.mark.parametrize("alias", CENSURADOS)
def test_q38_flash_censurado_no_pasa_por_el_gate(hook, alias):
    """El gate es de la ruta ABLITERADA; los censurados no la tocan."""
    assert alias not in hook.UNCENSORED_GATED_ALIASES, alias
    assert hook._uncensored_access_denied(alias, "cualquier-key") == (False, None)


# ── 4. sellos: `-u` con el suyo, censurados con el refusal:0 heredado ───────────

@pytest.mark.parametrize("alias", ABLITERADOS)
def test_q38_flash_uncensored_lleva_sello_propio_refusal_1(model_list, alias):
    body = model_list[alias]["litellm_params"].get("extra_body") or {}
    assert body == {"cache_salt": "refusal:1.0"}, (alias, body)


@pytest.mark.parametrize("alias", CENSURADOS)
def test_q38_flash_censurado_hereda_refusal_0_del_ancla(model_list, alias):
    body = model_list[alias]["litellm_params"].get("extra_body") or {}
    assert body == {"cache_salt": "refusal:0"}, (alias, body)


# ── 5. fallbacks: arista para los censurados, NINGUNA para los `-u` ─────────────

def test_q38_flash_censurados_tienen_arista_a_alibaba(config):
    graph = {
        source: destinations
        for entry in (config.get("router_settings") or {}).get("fallbacks") or []
        for source, destinations in entry.items()
    }
    for alias in CENSURADOS:
        assert graph.get(alias) == ["alibaba-q38-flash"], (alias, graph.get(alias))


@pytest.mark.parametrize("alias", ABLITERADOS)
def test_q38_flash_uncensored_no_tiene_arista_ni_cero(config, alias):
    """Una peticion abliterada NUNCA degrada: el fallback no hereda el
    `cache_salt` y Alibaba contestaria con el modelo base. La ausencia de arista
    es la capa 1; el stamp `disable_fallbacks` (cubierta en
    test_uncensored_no_fallback_contract.py, que lista estos nombres) es la 2."""
    graph = {
        source
        for entry in (config.get("router_settings") or {}).get("fallbacks") or []
        for source in entry
    }
    assert alias not in graph, alias


# ── 6. FORWARD_REASONING_MODELS y STRICT_LEADING_SYSTEM_MODELS, los cuatro ──────

@pytest.mark.parametrize("alias", sorted(CHAT_PROFILES))
def test_q38_flash_alias_en_forward_y_strict(hook, alias):
    """Indexadas por el nombre RESUELTO, y estos alias llegan con el suyo (no se
    reescriben). Sin FORWARD se pierde el `reasoning_content` del historial que
    Open WebUI reenvia (degrada el prefijo de cache); sin STRICT, el system fuera
    de sitio pega con el 400 del template."""
    assert alias in hook.FORWARD_REASONING_MODELS, alias
    assert alias in hook.STRICT_LEADING_SYSTEM_MODELS, alias


# ── menu publicado de los `-u`: espejo honesto del directo uncensored ───────────

@pytest.mark.parametrize("alias", ABLITERADOS)
def test_q38_flash_uncensored_espeja_el_menu_del_directo(model_list, alias):
    directo = model_list["qwen38-flash-next-uncensored"]["model_info"][
        "supported_reasoning_efforts"
    ]
    assert list(model_list[alias]["model_info"]["supported_reasoning_efforts"]) == list(directo)
