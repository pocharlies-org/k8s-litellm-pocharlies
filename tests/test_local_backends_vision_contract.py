"""Los backends locales declaran supports_vision, y DeepSeek en particular.

POR QUE EXISTE ESTE TEST (2026-08-13)
------------------------------------
`deepseek-v4-flash-tp2` estuvo con `"supports_vision": False` con el comentario
"DeepseekV4ForCausalLM: solo texto, no es multimodal". Era cierto hasta que el
servidor paso a servir `DeepseekV4VisionForCausalLM` (plugin FlyCockpit: tower
DeepEncoderV2 + adapter). A partir de ahi el flag quedo MINTIENDO, y el efecto no
fue un error sino algo peor:

  - `_vision_target` desviaba TODA peticion con imagen a
    `cloudblue/gpt-5.6-luna`, que gasta
    la cuota de ChatGPT del usuario;
  - y OpenClaw/OpenChamber, que leen la misma capacidad, contestaban "este modelo
    no admite entrada de imagenes" SIN LLEGAR A PREGUNTARLE AL MODELO.

O sea: el modelo veia perfectamente y la unica pieza rota era este booleano. Nada
en los logs lo delataba. Este test fija el valor para que volver a ponerlo en
False sea un fallo de CI y no un descubrimiento por sorpresa dentro de un mes.

ALCANCE: este test ancla SOLO a DeepSeek.

Los otros tres backends declarados (ornith-dgx1, nvidia-qwen36-dgx1,
qwen36-27b-uncensored-dgx2) tambien tienen supports_vision=True, pero NO se fijan
aqui, por dos razones comprobadas el 2026-08-13:

  - Los tres estan MUERTOS. `vllm-ornith-35b-nvfp4-mtp-dgx1` y
    `vllm-nvidia-qwen36-35b-dgx1` ni siquiera existen como Deployment (Ornith se
    retiro el 10-08), y `vllm-qwen36-27b-uncensored` esta a 0 replicas.
    Ninguno aparece registrado en LiteLLM: de los backends locales solo responde
    `deepseek-v4-flash-0731`, con sus 13 alias.
  - Sus valores no estan igual de justificados. El de NVIDIA lleva su razon en el
    codigo ("qwen3_5_moe MULTIMODAL"), pero el del 27B DENSO viene copiado del
    bloque anterior en mayo, sin comprobacion propia y sin comentario. Fijarlo
    seria convertir en invariante algo que nadie midio.

Cuando alguno vuelva a estar vivo, lo correcto es MEDIR si ve — mandarle una
imagen — y entonces anclarlo. Un test que fija un valor no verificado no protege
nada: solo hace mas dificil corregirlo.
"""
import ast
import pathlib

import yaml


MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# Todo backend LOCAL declarado en el controlador. Si se anade uno nuevo hay que
# tocar esta lista a proposito: es justo el momento de decidir si ve o no.
# 2026-08-13: retirados `ornith-dgx1` y `nvidia-qwen36-dgx1`. Estaban a replicas 0
# Y SIN PESOS EN DISCO (Ornith borrado el 10-08; la carpeta
# nvidia-qwen36-35b-a3b-nvfp4 no existe en dgx1), o sea que no podian arrancar.
# `qwen36-27b-uncensored-dgx2` se CONSERVA aunque su checkpoint tampoco este:
# es el unico dueño declarado de dense/dense-reasoning/dense-uncensored/taxonomy,
# y es preferible que la config diga "este backend deberia servir dense y esta
# caido" a que esos nombres no tengan dueño en ningun sitio.
BACKENDS_LOCALES = {
    "qwen36-27b-uncensored-dgx2",
    "deepseek-v4-flash-tp2",
    "qwen35-4b-int4",
}


def _codigo_del_sync() -> str:
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    for doc in docs:
        if doc.get("kind") != "ConfigMap":
            continue
        for nombre, contenido in (doc.get("data") or {}).items():
            if "BACKENDS = (" in contenido and "managed_model_contract" in contenido:
                return contenido
    raise AssertionError("no encuentro el codigo del backend-sync en el manifiesto")


def _backends() -> dict[str, dict]:
    """Extrae BACKENDS parseando el AST, sin ejecutar el modulo.

    Los valores que vienen de os.getenv() no se resuelven (no hacen falta aqui);
    solo interesan los literales como supports_vision.
    """
    arbol = ast.parse(_codigo_del_sync())
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Assign):
            continue
        destinos = [t.id for t in nodo.targets if isinstance(t, ast.Name)]
        if "BACKENDS" not in destinos:
            continue
        salida = {}
        for elemento in nodo.value.elts:
            entrada = {}
            for clave, valor in zip(elemento.keys, elemento.values):
                if not isinstance(clave, ast.Constant):
                    continue
                try:
                    entrada[clave.value] = ast.literal_eval(valor)
                except ValueError:
                    entrada[clave.value] = "<dinamico>"
            salida[entrada["name"]] = entrada
        return salida
    raise AssertionError("BACKENDS no es una asignacion literal en el sync")


def test_estan_todos_los_backends_locales_esperados():
    assert set(_backends()) == BACKENDS_LOCALES, (
        "cambio la lista de backends locales: revisa si el nuevo ve o no antes "
        "de tocar BACKENDS_LOCALES"
    )


def test_deepseek_declara_que_ve():
    ds = _backends()["deepseek-v4-flash-tp2"]
    assert ds["supports_vision"] is True, (
        "deepseek-v4-flash-tp2 sirve DeepseekV4VisionForCausalLM y SI ve. Con "
        "False, _vision_target desvia toda imagen a cloudblue/gpt-5.6-luna y los clientes "
        "responden 'no admite imagenes' sin preguntar al modelo."
    )


def test_todo_backend_local_declara_vision_explicitamente():
    """El campo tiene que ESTAR, con el valor que sea.

    No se fija el valor de los backends que no son DeepSeek: ver el ALCANCE del
    docstring del modulo. Lo que si es invariante es que ninguno se quede sin
    declararlo, porque un backend sin el campo deja `_alias_supports_vision` en
    None y el desvio de imagenes pasa a depender de si el alias esta vivo.
    """
    sin_campo = [n for n, b in _backends().items() if "supports_vision" not in b]
    assert not sin_campo, (
        f"backends locales sin declarar supports_vision: {sin_campo}. Declara el "
        "valor a proposito: True solo si se ha COMPROBADO que el modelo ve."
    )


def test_el_reconciler_refresca_el_flag_al_cambiarlo():
    """supports_vision tiene que estar en managed_model_contract.

    `/model/new` de LiteLLM es create-only: si el campo no esta en el contrato,
    un ID ya existente conserva el valor viejo PARA SIEMPRE y cambiar el
    manifiesto no surte efecto hasta borrar el deployment a mano.
    """
    codigo = _codigo_del_sync()
    inicio = codigo.index("def managed_model_contract")
    contrato = codigo[inicio:codigo.index("def add_model")]
    assert '"supports_vision": info.get("supports_vision")' in contrato, (
        "supports_vision fuera del contrato gestionado: el flag se quedaria "
        "pegado al valor con el que se registro por primera vez"
    )
