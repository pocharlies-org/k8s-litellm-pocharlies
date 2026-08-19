"""Los alias `-uncensored` sellan cada peticion con su lambda, y solo ellos.

POR QUE NO BASTA UN ASSERT DE TEXTO. Los demas contract tests de este repo
comprueban que ciertas cadenas estan en el manifiesto. Aqui eso no vale: el fallo
que este test existe para cazar no es un nombre que falte, es un `return`
temprano. `extra_litellm_params` tenia el guard de repeticion de Qwen ANTES del
sello, asi que el dia que Qwen tuviera lambda por peticion su alias habria salido
con el guard puesto y SIN sello, sin un solo aviso — igual de silencioso que el
bug del propio lambda por peticion. Un assert de texto pasa igual.

Asi que este test EXTRAE la funcion del ConfigMap y la EJECUTA. Si alguien vuelve
a poner un `return` donde habia un `params.update(...)`, el caso 4 falla.

CONTRA QUE SE PROTEGE, en una linea cada uno:
  1. que el alias normal empiece a sellar (seria censura retirada por accidente)
  2. que el alias -uncensored deje de sellar (seria un alias que MIENTE: dice
     uncensored y sirve el modelo censurado — el fallo nº1 de esta familia)
  3. que un alias de capacidad (`tooling`, `agent`, `high`) herede el sello
  4. que el sello y el guard de familia se excluyan entre si
  5. que el lambda de un modelo se aplique al otro. NO son intercambiables:
     medido, DeepSeek necesita 1.5 (a 1.0 aun rechaza 5-7/10) y Qwen3.8 se
     enciende con 1.0 (a 1.5 pierde 26,8 puntos de MMLU-Pro, p=0,0000).
"""
import ast
import logging
import os
import textwrap
from pathlib import Path

import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"

# Nombres que hay que sacar del ConfigMap para poder ejecutar la funcion suelta.
_WANTED = {
    "UNCENSORED_SUFFIX",
    "UNCENSORED_ON_LAMBDA",
    "QWEN38_REPEAT_GUARD_PARAMS",
    "QWEN38_27B_ALIASES",
    "DEEPSEEK_V4_FLASH_DIRECT_ALIASES",
}

DEEPSEEK = {"base_model": "openai/deepseek-v4-flash-0731"}
QWEN38 = {"base_model": "openai/qwen38-27b"}


def _load_sync_namespace():
    """Compila SOLO las constantes y `extra_litellm_params` del sync.py embebido.

    No se importa el modulo entero a proposito: arranca hilos, habla con la API de
    Kubernetes y con LiteLLM. Lo que se quiere probar es una funcion pura.
    """
    docs = list(yaml.safe_load_all(MANIFEST.read_text()))
    src = textwrap.dedent(
        next(
            value
            for doc in docs
            if doc and doc.get("kind") == "ConfigMap"
            for key, value in (doc.get("data") or {}).items()
            if key == "sync.py"
        )
    )
    tree = ast.parse(src)
    picked, fn = [], None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _WANTED for t in node.targets
        ):
            picked.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == "extra_litellm_params":
            fn = node
    assert fn is not None, "extra_litellm_params desaparecio del sync.py"
    missing = _WANTED - {
        t.id for n in picked for t in n.targets if isinstance(t, ast.Name)
    }
    assert not missing, f"faltan constantes en el sync.py: {sorted(missing)}"

    module = ast.Module(body=picked + [fn], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": object,
        "log": logging.getLogger("test_uncensored_alias"),
        "os": os,
        # Los alias de capacidad se construyen a partir de estas tuplas en el
        # sync real; aqui basta con que existan para que el modulo compile.
        "TOOLING_COMPAT_ALIASES": (),
        "THINKING_TIER_ALIASES": (),
    }
    exec(compile(module, "<sync.py>", "exec"), namespace)  # noqa: S102
    return namespace


def test_only_the_uncensored_alias_carries_the_per_request_salt():
    ns = _load_sync_namespace()
    extra = ns["extra_litellm_params"]

    # 1 — el nombre directo no sella. Es el modelo tal cual.
    assert extra(DEEPSEEK, "deepseek-v4-flash-0731") == {}

    # 2 — el alias -uncensored sella con SU lambda.
    assert extra(DEEPSEEK, "deepseek-v4-flash-0731-uncensored") == {
        "extra_body": {"cache_salt": "refusal:1.5"}
    }

    # 3 — un alias de capacidad enruta al mismo backend y NO hereda el sello.
    assert extra(QWEN38, "tooling") == {"repetition_penalty": 1.08}

    # 4 — EL CASO DEL `return` TEMPRANO: guard de familia Y sello, no uno u otro.
    assert extra(QWEN38, "qwen38-27b-uncensored") == {
        "repetition_penalty": 1.08,
        "extra_body": {"cache_salt": "refusal:1.0"}
    }

    # 5 — los lambdas no se cruzan entre modelos.
    assert ns["UNCENSORED_ON_LAMBDA"] == {
        "openai/deepseek-v4-flash-0731": 1.5,
        "openai/qwen38-27b": 1.0,
    }


def test_the_uncensored_aliases_are_actually_registered():
    """Un sello que nadie puede pedir no sirve de nada: el alias tiene que estar
    en la lista que el reconciler manda a `/model/new`."""
    ns = _load_sync_namespace()
    suffix = ns["UNCENSORED_SUFFIX"]

    assert suffix == "-uncensored"
    assert "deepseek-v4-flash-0731-uncensored" in ns["DEEPSEEK_V4_FLASH_DIRECT_ALIASES"]
    assert "qwen38-27b-uncensored" in ns["QWEN38_27B_ALIASES"]

    # `tooling` sigue siendo el alias de capacidad de Qwen y no se ha renombrado.
    assert "tooling" in ns["QWEN38_27B_ALIASES"]


def test_cache_salt_is_not_stripped_by_the_family_sampling_hook():
    """FAMILY_SAMPLING borra claves de sampling por familia. Si `cache_salt`
    entrara en un `drop`, el alias moriria en silencio en el hook en vez de en el
    registro, que es mucho mas dificil de ver."""
    text = MANIFEST.read_text()
    families = text[text.index("FAMILY_SAMPLING = {"):]
    families = families[: families.index("\n    }\n") + 7]
    assert "cache_salt" not in families
