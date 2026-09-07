"""Los alias locales declaran supports_vision, y el residente llm-tp en particular.

POR QUE EXISTE ESTE TEST (2026-08-13)
------------------------------------
Nacio anclando a `deepseek-v4-flash-tp2`, que estuvo con `"supports_vision": False`
y el comentario "DeepseekV4ForCausalLM: solo texto, no es multimodal". Era cierto
hasta que el servidor paso a servir `DeepseekV4VisionForCausalLM` (plugin
FlyCockpit: tower DeepEncoderV2 + adapter). A partir de ahi el flag quedo
MINTIENDO, y el efecto no fue un error sino algo peor:

  - `_vision_target` desviaba TODA peticion con imagen al fallback configurado;
  - y OpenClaw/OpenChamber, que leen la misma capacidad, contestaban "este modelo
    no admite entrada de imagenes" SIN LLEGAR A PREGUNTARLE AL MODELO.

O sea: el modelo veia perfectamente y la unica pieza rota era este booleano. Nada
en los logs lo delataba. Este test fija el valor para que volver a ponerlo en
False sea un fallo de CI y no un descubrimiento por sorpresa dentro de un mes.

01-09-2026: DeepSeek-V4-Flash se retira del cluster y el ancla pasa a
`qwen38-flash-next`, que hereda el papel de residente llm-tp Y de destino de
LITELLM_VISION_FALLBACK_MODEL. El riesgo es el mismo pero mas agudo: si su flag
miente, el desvio de imagenes apunta AL MISMO backend y no hay a donde caer.

07-09-2026: hasta hoy este test leia el `BACKENDS` del ConfigMap de
`litellm-dgx-backend-sync`. Ese controlador llevaba muerto desde el 18-08 y se
borro del repo, asi que el test validaba una tabla que nada ejecutaba. La unidad
pasa a ser el ALIAS del `model_list` estatico, que es donde vive el flag que leen
de verdad `_alias_supports_vision`, OpenClaw y OpenChamber.

ALCANCE: se fija el valor SOLO del residente llm-tp vivo. De los demas se exige
que declaren el campo, no un valor concreto: fijar un booleano que nadie ha
medido no protege nada, solo hace mas dificil corregirlo. Cuando alguno se mida
—mandarle una imagen— es el momento de anclarlo aqui.
"""
import pathlib

import yaml


MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# Todo alias de CHAT servido dentro del cluster. Si se anade uno nuevo hay que
# tocar esta lista a proposito: es justo el momento de decidir si ve o no.
ALIAS_LOCALES = {
    "qwen38-flash-next",
    "qwen38-flash-next-uncensored",
    "qwen38-27b",
    "qwen38-27b-uncensored",
    "qwen35-4b",
    "qwen35-4b-fast",
    "tooling",
    "tooling-uncensored",
}


def _config() -> dict:
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return yaml.safe_load(doc["data"]["config.yaml"])
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def _alias_locales() -> dict[str, dict]:
    """{model_name: model_info} de los alias de chat servidos en el cluster.

    El discriminador es el `api_base`: los alias de nube no pasan por nuestros
    backends y su capacidad la declara el upstream.
    """
    salida = {}
    for entrada in _config()["model_list"]:
        params = entrada.get("litellm_params") or {}
        info = entrada.get("model_info") or {}
        if ".llm.svc.cluster.local" not in str(params.get("api_base") or ""):
            continue
        if info.get("mode") != "chat":
            continue
        salida[entrada["model_name"]] = info
    return salida


def test_estan_todos_los_alias_locales_esperados():
    assert set(_alias_locales()) == ALIAS_LOCALES, (
        "cambio la lista de alias locales de chat: revisa si el nuevo ve o no "
        "antes de tocar ALIAS_LOCALES"
    )


def test_qwen38_flash_next_declara_que_ve():
    """01-09-2026: hereda el ancla que tenia DeepSeek-V4-Flash al retirarse.

    Y aqui importa mas que antes: al irse DeepSeek, este backend pasa a ser el
    destino de LITELLM_VISION_FALLBACK_MODEL. Ver el checkpoint:
    `quantization_config.ignore` incluye `model.visual.*`, sus 333 tensores
    visuales no llevan weight_scale (van en BF16), y vLLM registra
    Qwen4ExpForConditionalGeneration en _MULTIMODAL_MODELS. El servidor arranca
    SIN --language-model-only desde el mismo cambio.

    Comprobado contra el pod: describe correctamente una imagen de prueba.
    """
    for alias in ("qwen38-flash-next", "qwen38-flash-next-uncensored"):
        assert _alias_locales()[alias]["supports_vision"] is True, (
            f"{alias} es el backend de vision desde que se retiro DeepSeek. Con "
            "False, _vision_target desvia toda imagen al fallback -- que es EL "
            "MISMO backend -- y los clientes responden 'no admite imagenes' sin "
            "preguntar al modelo."
        )


def test_todo_alias_local_declara_vision_explicitamente():
    """El campo tiene que ESTAR, con el valor que sea.

    No se fija el valor de los que no son el residente llm-tp: ver el ALCANCE del
    docstring del modulo. Lo que si es invariante es que ninguno se quede sin
    declararlo, porque un alias sin el campo deja `_alias_supports_vision` en
    None y el desvio de imagenes pasa a depender de si el alias esta vivo.
    """
    sin_campo = [n for n, i in _alias_locales().items() if "supports_vision" not in i]
    assert not sin_campo, (
        f"alias locales sin declarar supports_vision: {sin_campo}. Declara el "
        "valor a proposito: True solo si se ha COMPROBADO que el modelo ve."
    )
