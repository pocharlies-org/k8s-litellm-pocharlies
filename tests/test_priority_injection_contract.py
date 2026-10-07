"""DGX-453 (2026-09-29): prioridad de cola del residente por alias de clave.

El hook `session_router.apply_priority` (EMBED del ConfigMap `litellm-config`,
fuente de verdad del modulo) inyecta `priority` en `extra_body` SOLO hacia el
residente (`*.llm.svc.cluster.local`) y SOLO para alias listados en el mapa
top-level `priority_by_alias` de config.yaml. El mapa vive en CONFIG, no en
codigo: reordenar la cola es editar el manifest, no este modulo.

Misma tecnica que tests/test_alibaba_account_affinity.py: el modulo se carga
del EMBED del manifest, no de dev/ (dev/ es solo la copia leible).
"""
import os
import sys
import types
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _configmap():
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    return next(d for d in docs
                if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")


ROUTER_SRC = _configmap()["data"]["session_router.py"]
STRIP_SRC = _configmap()["data"]["litellm_strip_params.py"]
CONFIG_YAML = yaml.safe_load(_configmap()["data"]["config.yaml"])


def _router():
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        mod = types.ModuleType("session_router_priority")
        exec(compile(ROUTER_SRC, "session_router.py", "exec"), mod.__dict__)
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)
    return mod


CFG = """
model_list:
- model_name: qwen38-flash-next
  litellm_params:
    model: openai/qwen
    api_base: http://qwen38-flash-next.llm.svc.cluster.local:8000/v1
- model_name: tooling
  litellm_params:
    model: openai/tool
    api_base: http://tooling.llm.svc.cluster.local:8000/v1
- model_name: alibaba-q38-flash
  litellm_params:
    model: openai/qwen
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
- model_name: openrouter-claude
  litellm_params:
    model: anthropic/claude
    api_base: https://openrouter.ai/api/v1
- model_name: qwen38-flash-next-uncensored
  litellm_params:
    model: openai/qwen
    api_base: http://qwen38-flash-next.llm.svc.cluster.local:8000/v1
- model_name: qwen38-nube-uncensored
  litellm_params:
    model: openai/qwen
    api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
priority_by_alias:
  open-webui-v3: -10
  hermes: -10
  hermes-batch: 10
  neutra: 0
  claude-local:
    default: -5
    company: 5
  roto-por-clase:
    default: no-es-entero
priority_uncensored: -20
"""


def _mod_with_config(tmp_path, content=CFG, name="config.yaml"):
    p = tmp_path / name
    p.write_text(content)
    mod = _router()
    mod._PRIORITY_CONFIG_PATH = str(p)
    return mod, p


def _data(model, alias="open-webui-v3", cls=None):
    d = {"model": model}
    meta = {}
    if alias is not None:
        meta["user_api_key_alias"] = alias
    if cls is not None:
        # La estampa el wrapper de la compania (x-claude-class); el router la lee
        # de las tres fuentes que documenta _claude_class. Aqui, la de siempre.
        meta["headers"] = {"x-claude-class": cls}
    if meta:
        d["metadata"] = meta
    return d


# ── niveles ───────────────────────────────────────────────────────────────────


def test_alias_de_chat_y_bots_salen_por_delante_con_nivel_negativo(tmp_path):
    mod, _ = _mod_with_config(tmp_path)
    for alias in ("open-webui-v3", "hermes"):
        d = _data("qwen38-flash-next", alias)
        mod.apply_priority(d)
        assert d["extra_body"]["priority"] == -10


def test_alias_de_lote_sale_con_nivel_positivo(tmp_path):
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "hermes-batch")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == 10


def test_prioridad_cero_no_inyecta_nada(tmp_path):
    # 0 es el default de vLLM: anadirlo seria ruido en el request.
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "neutra")
    mod.apply_priority(d)
    assert "extra_body" not in d


# ── DGX-601: niveles por CLASE de sesion dentro de un alias ──────────────────


def test_alias_por_clase_sin_clave_uso_personal_sale_por_delante(tmp_path):
    # `claude-local` comparte key para la compania y para el uso personal: sin
    # clase (la de Dani) sale con el `default` del mapa, por delante del resto.
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "claude-local")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == -5


def test_alias_por_clase_company_sale_un_poco_por_detras_pero_antes_del_lote(tmp_path):
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "claude-local", cls="company")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == 5
    # el lote (hermes-batch: 10) sigue por detras: menor numero = antes
    lote = _data("qwen38-flash-next", "hermes-batch")
    mod.apply_priority(lote)
    assert lote["extra_body"]["priority"] > d["extra_body"]["priority"]


def test_alias_por_clase_con_clase_no_listada_usa_el_default(tmp_path):
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "claude-local", cls="otra-cosa")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == -5


def test_alias_por_clase_compara_sin_importar_mayusculas(tmp_path):
    # El header llega con la caja del cable; _claude_class lo normaliza.
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "claude-local", cls="COMPANY")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == 5


def test_forma_por_clase_rota_fail_open(tmp_path):
    # Ningun nivel entero util en el mapa => ese alias no inyecta nada.
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "roto-por-clase")
    mod.apply_priority(d)
    assert "extra_body" not in d


# ── DGX-601: lo uncensored pasa por delante de todo ───────────────────────────


def test_uncensored_manda_sobre_el_mapa_de_alias_con_qualquier_key(tmp_path):
    # El motivo: no tiene proveedor de alternativa, si espera no se sirve en otro
    # sitio. Da igual que la key sea la del lote (10) o la de la compania (5).
    mod, _ = _mod_with_config(tmp_path)
    for alias, cls in (("hermes-batch", None), ("claude-local", "company"),
                       ("claude-local", None), ("sin-mapear", None)):
        d = _data("qwen38-flash-next-uncensored", alias, cls=cls)
        mod.apply_priority(d)
        assert d["extra_body"]["priority"] == -20, (alias, cls)


def test_uncensored_a_destino_cloud_no_inyecta(tmp_path):
    # El gate del residente va primero: un nombre con el sufijo no basta si el
    # api_base resuelto no es del residente.
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-nube-uncensored", "hermes-batch")
    mod.apply_priority(d)
    assert "extra_body" not in d


def test_uncensored_sin_nivel_configurado_no_inyecta_por_ser_uncensored(tmp_path):
    # `priority_uncensored` ausente => se comporta como antes de DGX-601.
    mod, _ = _mod_with_config(
        tmp_path, content=CFG.replace("priority_uncensored: -20", ""))
    d = _data("qwen38-flash-next-uncensored", "claude-local")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == -5   # por alias, no por ser uncensored


# ── sin alias / alias no mappeado ─────────────────────────────────────────────


def test_sin_alias_no_se_anaede_el_campo(tmp_path):
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", None)
    mod.apply_priority(d)
    assert "extra_body" not in d


def test_alias_no_mappeado_no_se_anaede_el_campo(tmp_path):
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "otra-clave")
    mod.apply_priority(d)
    assert "extra_body" not in d


# ── destino: solo el residente ────────────────────────────────────────────────


def test_destino_cloud_no_inyecta_alibaba_ni_openrouter(tmp_path):
    # Pueden devolver 400: el campo no existe en su API.
    mod, _ = _mod_with_config(tmp_path)
    for model in ("alibaba-q38-flash", "openrouter-claude"):
        d = _data(model, "hermes")
        mod.apply_priority(d)
        assert "extra_body" not in d, model


def test_fallback_a_alibaba_q38_flash_sale_sin_campo(tmp_path):
    # El caso del desvio/overflow: alias de chat, modelo ya resuelto a Alibaba.
    mod, _ = _mod_with_config(tmp_path)
    d = _data("alibaba-q38-flash", "open-webui-v3")
    mod.apply_priority(d)
    assert "extra_body" not in d


def test_resuelve_por_el_modelo_resuelto_no_por_el_pedido(tmp_path):
    # data["model"] es lo que mira el hook (tras la red final): cualquier
    # deployment del residente vale, aqui `tooling`.
    mod, _ = _mod_with_config(tmp_path)
    d = _data("tooling", "hermes")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == -10


def test_extra_body_preexistente_se_conserva_y_no_se_pisa_un_priority_puesto(tmp_path):
    mod, _ = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "hermes")
    d["extra_body"] = {"cache_salt": "refusal:0", "priority": 5}
    mod.apply_priority(d)
    assert d["extra_body"] == {"cache_salt": "refusal:0", "priority": 5}


# ── fail-open total ───────────────────────────────────────────────────────────


def test_config_ausente_fail_open(tmp_path):
    mod = _router()
    mod._PRIORITY_CONFIG_PATH = str(tmp_path / "no-existe.yaml")
    d = _data("qwen38-flash-next", "hermes")
    mod.apply_priority(d)  # no lanza
    assert "extra_body" not in d


def test_config_corrupta_fail_open(tmp_path):
    mod, _ = _mod_with_config(tmp_path, content="{{{roto ::")
    d = _data("qwen38-flash-next", "hermes")
    mod.apply_priority(d)  # no lanza
    assert "extra_body" not in d


def test_mapa_con_forma_rara_fail_open(tmp_path):
    mod, _ = _mod_with_config(
        tmp_path, content="priority_by_alias: [no, soy, un, mapa]\nmodel_list: []\n")
    d = _data("qwen38-flash-next", "hermes")
    mod.apply_priority(d)
    assert "extra_body" not in d


def test_cache_no_reparsea_sin_cambio_de_mtime(tmp_path):
    # El config cambia en disco pero el mtime no se mueve: el hook sigue con el
    # mapa viejo (es el precio de la cache; el reload real es por mtime).
    mod, p = _mod_with_config(tmp_path)
    d = _data("qwen38-flash-next", "hermes")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == -10
    st = os.stat(p)
    p.write_text(CFG.replace("hermes: -10", "hermes: -99"))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    d = _data("qwen38-flash-next", "hermes")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == -10
    # y con mtime nuevo, reparsea:
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    d = _data("qwen38-flash-next", "hermes")
    mod.apply_priority(d)
    assert d["extra_body"]["priority"] == -99


# ── el mapa vive en config, no hardcodeado en el modulo ───────────────────────


def test_el_mapa_vive_en_config_yaml():
    assert CONFIG_YAML["priority_by_alias"] == {
        "open-webui-v3": -10, "hermes": -10, "hermes-batch": 10, "brain": 20,
        # DGX-601 (Dani): `claude-local` es la misma key para la compania y para el
        # uso personal, asi que su nivel va MAPA por clase, no entero.
        "claude-local": {"default": -5, "company": 5},
    }
    # DGX-601: lo uncensored no tiene alternativa -> nivel propio, por delante.
    assert CONFIG_YAML["priority_uncensored"] == -20


def test_el_modulo_no_hardcodea_alias_ni_niveles():
    # La decision es CONFIG: si un alias o un nivel aparece literal en el
    # modulo, alguien lo movio a codigo y este test lo para.
    # Excepcion (DGX-454, C7): la valvula KV lista `hermes-batch` como DEFAULT de
    # bot_keys (DEFAULT_BOT_KEYS, contrato dgx.model-routing.config.v2). Es otra
    # decision (umbral de KV de los bots), no el mapa de prioridad: las lineas que
    # hablan de bot_keys no cuentan; cualquier otra aparicion sigue parando aqui.
    # Segunda excepcion (SC-2082 P6c): BOT_BURST_KEYS, la lista FIJA de la key de los
    # bots de la compania que manda el BURST de Alibaba. Tampoco es prioridad.
    src = "\n".join(l for l in ROUTER_SRC.splitlines()
                    if "bot_keys" not in l.lower() and "BOT_BURST_KEYS =" not in l)
    for token in ("open-webui-v3", "hermes-batch", "priority_by_alias: {",
                  "-10", "+10"):
        assert token not in src, f"{token!r} hardcodeado en session_router"
    assert "hermes" not in ROUTER_SRC.split("apply_priority")[1]


# ── el hook llama a apply_priority en la posicion acordada ────────────────────


def test_el_hook_llama_apply_priority_despues_del_pin_de_afinidad():
    src = STRIP_SRC
    call = "session_router.apply_priority(data)"
    assert call in src
    assert src.index("session_router.stamp_alibaba_session_affinity(data)") \
        < src.index(call), "apply_priority debe ir DESPUES del pin de afinidad"
    trozo = src[src.rindex("if session_router is not None:", 0, src.index(call)):]
    assert "session_router is not None" in trozo.split(call)[0], \
        "la llamada va gateada por session_router is not None"
