"""Los backends locales declaran supports_vision, y DeepSeek en particular.

POR QUE EXISTE ESTE TEST (2026-08-13)
------------------------------------
`deepseek-v4-flash-tp2` estuvo con `"supports_vision": False` con el comentario
"DeepseekV4ForCausalLM: solo texto, no es multimodal". Era cierto hasta que el
servidor paso a servir `DeepseekV4VisionForCausalLM` (plugin FlyCockpit: tower
DeepEncoderV2 + adapter). A partir de ahi el flag quedo MINTIENDO, y el efecto no
fue un error sino algo peor:

  - `_vision_target` desviaba TODA peticion con imagen a `gpt-5.6-sol`, que gasta
    la cuota de ChatGPT del usuario;
  - y OpenClaw/OpenChamber, que leen la misma capacidad, contestaban "este modelo
    no admite entrada de imagenes" SIN LLEGAR A PREGUNTARLE AL MODELO.

O sea: el modelo veia perfectamente y la unica pieza rota era este booleano. Nada
en los logs lo delataba. Este test fija el valor para que volver a ponerlo en
False sea un fallo de CI y no un descubrimiento por sorpresa dentro de un mes.

Los otros tres backends locales son checkpoints qwen3_5_moe, multimodales de
fabrica; se comprueban igual porque el modo de fallo es identico.
"""
import ast
import pathlib

import yaml


MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# Todo backend LOCAL declarado en el controlador. Si se anade uno nuevo hay que
# tocar esta lista a proposito: es justo el momento de decidir si ve o no.
BACKENDS_LOCALES = {
    "ornith-dgx1",
    "nvidia-qwen36-dgx1",
    "qwen36-27b-uncensored-dgx2",
    "deepseek-v4-flash-tp2",
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
        "False, _vision_target desvia toda imagen a gpt-5.6-sol y los clientes "
        "responden 'no admite imagenes' sin preguntar al modelo."
    )


def test_todos_los_backends_locales_declaran_vision():
    sin_declarar = {
        nombre: b.get("supports_vision")
        for nombre, b in _backends().items()
        if b.get("supports_vision") is not True
    }
    assert not sin_declarar, (
        f"estos backends locales no declaran vision: {sin_declarar}. Un False "
        "aqui no da error en ningun sitio: desvia las imagenes en silencio."
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
