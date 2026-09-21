"""Contract for the single dynamic local capability: ``tooling``.

The capability follows the resident that is operational according to the GPU
arbiter's component readiness. It has no cloud or cross-profile fallback.
"""
import ast
import types
from pathlib import Path

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
WANT_FN = {
    "_component_is_ready",
    "_ready_tooling_modes",
    "_select_ready_tooling_mode",
    "_tooling_target_for_compute_mode",
    "_tooling_route_for_state",
}
WANT_CONST = {
    "TOOLING_MODE_TARGETS",
    "TOOLING_MODE_COMPONENTS",
    "TOOLING_FALLBACKS",
}


@pytest.fixture(scope="module")
def hook():
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    source = next(
        doc["data"]["litellm_strip_params.py"]
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )
    keep = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FN:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") in WANT_CONST for target in node.targets
        ):
            keep.append(node)
    module = types.ModuleType("tooling_profile")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), module.__dict__)
    return module


def _state(*, deepseek=False, qwen=False, desired="llm-tp", effective="llm-tp", phase="ready"):
    component = lambda name, ready: {
        "name": name,
        "ready": ready,
        "desired_replicas": 1 if ready else 0,
        "ready_replicas": 1 if ready else 0,
    }
    return {
        "phase": phase,
        "desired_mode": desired,
        "effective_mode": effective,
        "components": {
            "dgx1": [
                component("deepseek-worker", deepseek),
                component("dense-uncensored", qwen),
            ],
            "dgx2": [component("deepseek-head", deepseek)],
        },
    }


def test_tooling_uses_the_tp2_resident_only_when_both_ranks_are_ready(hook):
    # 26-08: el residente llm-tp es qwen38-flash-next. Las keys de componente
    # del fixture (deepseek-head/worker) se quedan: son el contrato del
    # dashboard y hoy apuntan a los deploys qwen38-flash-next-*.
    state = _state(deepseek=True)
    assert hook._tooling_target_for_compute_mode(state) == (
        "qwen38-flash-next",
        None,
    )
    assert hook._tooling_route_for_state(state, lambda name: name == "qwen38-flash-next") == (
        "qwen38-flash-next",
        "primary",
        None,
    )


def test_tooling_uses_qwen_when_qwen_is_the_ready_resident(hook):
    state = _state(qwen=True, desired="creative", effective="creative")
    assert hook._tooling_target_for_compute_mode(state) == ("qwen38-27b", None)
    assert hook._tooling_route_for_state(state, lambda name: name == "qwen38-27b") == (
        "qwen38-27b",
        "primary",
        None,
    )


def test_transition_keeps_whichever_resident_is_actually_ready(hook):
    state = _state(
        qwen=True,
        desired="llm-tp",
        effective="creative",
        phase="switching",
    )
    assert hook._tooling_target_for_compute_mode(state) == ("qwen38-27b", None)


def test_no_ready_resident_fails_closed_without_fallback(hook):
    state = _state()
    assert hook.TOOLING_FALLBACKS == ()
    assert hook._tooling_route_for_state(state, lambda _name: False) == (
        None,
        "dry",
        "tooling_resident_not_ready",
    )


def test_proxy_fallbacks_never_leave_local_models():
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    raw = next(
        doc["data"]["config.yaml"]
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )
    config = yaml.safe_load(raw)
    entries = config.get("router_settings", {}).get("fallbacks") or []
    graph = {
        source: destinations
        for entry in entries
        for source, destinations in entry.items()
    }
    # 24-08-2026: `high` y `max` se retiran del model_list —eran el mismo backend
    # con otro nivel de pensamiento, que hoy se pide con `reasoning_effort`— y con
    # ellos sus dos saltos, que eran los unicos. Un fallback declarado para un
    # alias que ya no existe no se dispara jamas. El grafo quedo VACIO.
    #
    # 20-09-2026: deja de estar vacio, con UNA arista y decidida a proposito por el
    # owner. `graph == {}` era la forma de escribir "un fallback no saca trafico de
    # los modelos locales" cuando NO habia gemelo en la nube al que caer; ahora lo
    # hay (plan Team de Alibaba Model Studio) y el owner pidio ese salto. Lo que el
    # contrato protege sigue siendo lo mismo, pero ahora se escribe explicito: la
    # lista blanca es de UNA entrada y todo lo demas sigue prohibido.
    #
    # OJO A LO QUE IMPLICA LA ARISTA: cuando dispara, el prompt SALE de la maquina
    # a un proveedor de terceros. Por eso la fuente permitida es solo el nombre
    # directo del residente y NO `tooling` (que debe fallar visible: es el perfil
    # global) ni ninguna ruta `-uncensored` (el sello `cache_salt: refusal:N` es una
    # extension de nuestro vLLM; Alibaba la ignora y la ruta perderia su proposito
    # EN SILENCIO). Ampliar esta lista es una decision del owner, no del que pasa.
    #
    # 21-09-2026 (OWU-51): la ampliacion ya esta tomada — el criterio C7 de la
    # epica OWU-50, APROBADO POR DANI, autoriza las dos aristas de los perfiles
    # CENSURADOS del chat (q38-flash / q38-flash-think) al mismo gemelo de nube.
    # Los `-u` siguen y seguiran sin arista: una peticion abliterada no degrada.
    PERMITIDAS = {
        "qwen38-flash-next": ["alibaba-q38-flash"],
        "q38-flash": ["alibaba-q38-flash"],
        "q38-flash-think": ["alibaba-q38-flash"],
    }
    assert graph == PERMITIDAS, (
        "el grafo de fallbacks solo admite las aristas aprobadas por el owner "
        f"(20-09 y 21-09-2026, OWU-50-C7); encontrado: {graph}"
    )

    # Ninguna ruta sellada puede tener fallback: perderia el sello sin avisar.
    assert not [src for src in graph if src.endswith("-uncensored")]

    # El destino tiene que EXISTIR en el model_list; un fallback a un alias que no
    # se publica no se dispara jamas y es peor que no tenerlo (ver el caso de
    # `high`/`max` de arriba).
    publicados = {m["model_name"] for m in config["model_list"]}
    for source, targets in graph.items():
        assert source in publicados, source
        for target in targets:
            assert target in publicados, target
            # `model_name`, nunca `proveedor/modelo`: el Router resuelve por alias.
            assert "/" not in target, target


def test_el_cooldown_del_residente_aparca_mas_que_el_de_su_fallback():
    """20-09-2026: cooldown ASIMETRICO, y a proposito.

    El residente local aparca LARGO porque caerse cuesta dinero: el trafico se va
    a un tercero de pago con la cache fria y se refactura el prompt entero (~81k
    tokens). Lo caro no es estar en Alibaba, es LLEGAR a Alibaba, asi que un
    residente que flapea con cooldown corto paga el prefijo en cada rebote.

    El fallback aparca CORTO porque es el ULTIMO recurso: expulsarlo mientras el
    residente sigue caido deja la peticion sin ningun sitio a donde ir.

    Si alguien iguala los dos valores, o invierte el orden, este test cae.
    """
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    raw = next(
        doc["data"]["config.yaml"]
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc["metadata"]["name"] == "litellm-config"
    )
    config = yaml.safe_load(raw)
    por_nombre = {m["model_name"]: m for m in config["model_list"]}

    residente = (por_nombre["qwen38-flash-next"].get("model_info") or {}).get("cooldown_time")
    fallback = (por_nombre["alibaba-q38-flash"].get("model_info") or {}).get("cooldown_time")
    router = config["router_settings"]["cooldown_time"]

    assert residente is not None and fallback is not None, (
        "los dos extremos del salto declaran su cooldown; si uno se cae al default "
        "del router la asimetria desaparece sin que se note"
    )
    assert fallback < router < residente, (
        f"orden esperado: fallback ({fallback}) < router ({router}) < residente ({residente})"
    )

    # `litellm_params` se copia ENTERO en los kwargs de la llamada al proveedor
    # (docstring de `_get_deployment_cooldown_policy`), asi que cooldown_time ahi
    # se filtraria a la peticion de salida. `_first_present` mira `model_info`
    # primero: ese es su sitio.
    for nombre in ("qwen38-flash-next", "alibaba-q38-flash"):
        assert "cooldown_time" not in por_nombre[nombre]["litellm_params"], (
            f"{nombre}: cooldown_time va en model_info, no en litellm_params"
        )


def test_los_alias_de_alibaba_caben_en_un_boton_de_telegram():
    """El id del alias ES la etiqueta del boton: si no cabe, Telegram recorta.

    El picker de Hermes va por teclado inline de DOS columnas (`_rows_of_two` en
    su adapter de Telegram, fijo, sin configuracion) y Hermes no admite etiqueta
    por modelo — `_declared_model_ids` solo lee las claves del mapa `models:` —,
    luego lo unico que se ve es el id. Medido en la captura de Dani (20-09):
    `qwen38-flash-next` (17) se leia entero y `alibaba-qwen38-max` (18) salia
    `alibaba-…38-max`. Por eso el techo aqui es 17.

    No es un capricho estetico: con el recorte Dani no distinguia q37-max de
    q37-plus y acabo eligiendo a ciegas desde el movil.
    """
    import yaml

    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    config = yaml.safe_load(cm["data"]["config.yaml"])

    anchos = {
        m["model_name"]: len(m["model_name"])
        for m in config["model_list"]
        if str(m["model_name"]).startswith("alibaba-")
    }
    assert anchos, "se han ido los alias de Alibaba del model_list"
    pasados = {n: w for n, w in anchos.items() if w > 17}
    assert not pasados, f"no caben en un boton de 2 columnas: {pasados}"


def test_el_puente_de_renombrado_no_apunta_a_la_nada():
    """Cada nombre viejo del puente tiene que seguir siendo un grupo publicado.

    `model_group_alias` resuelve ANTES de enrutar, asi que un destino mal escrito
    no da error de config: da un 400 en runtime con el nombre del cliente
    delante — el mismo fallo que este puente vino a curar. Y el puente tampoco
    puede quedarse para siempre: si el destino se renombra otra vez y el puente
    sigue, el nombre viejo apunta a un grupo que ya no existe.
    """
    import yaml

    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    config = yaml.safe_load(cm["data"]["config.yaml"])

    puente = (config.get("router_settings") or {}).get("model_group_alias") or {}
    publicados = {m["model_name"] for m in config["model_list"]}

    viejos = {k for k in puente if k.startswith("alibaba-")}
    assert viejos, "el puente del renombrado de Alibaba ha desaparecido sin avisar"
    colgados = {viejo: dest for viejo, dest in puente.items() if dest not in publicados}
    assert not colgados, f"alias viejos apuntando a un grupo inexistente: {colgados}"
    # y el destino corto es el que de verdad se sirve (no un tercer nombre)
    assert all(dest.startswith("alibaba-") for dest in puente.values())
