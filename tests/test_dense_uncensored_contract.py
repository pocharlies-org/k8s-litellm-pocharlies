import re
from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def _sync_block(text, start, end):
    return text[text.index(start):text.index(end)]


def test_uncensored_is_the_single_backend_owning_every_dense_alias():
    """Since the censored 27B F2 deployments were deleted (2026-07-26) the
    uncensored 27B is the cluster's ONLY dense model, so it owns every
    dense-shaped alias -- including `taxonomy`, which used to be registered by
    the deleted deployments and survived only because the hook rewrote it."""
    text = MANIFEST.read_text()

    assert "QWEN36_27B_UNCENSORED_ALIASES = (" in text
    aliases = _sync_block(text, "QWEN36_27B_UNCENSORED_ALIASES = (", "QWEN36_REPEAT_GUARD_PARAMS")
    for name in ('"dense-uncensored"', '"dense"', '"dense-reasoning"', '"taxonomy"'):
        assert name in aliases, f"{name} debe registrarlo el uncensored"

    assert text.count('"aliases": QWEN36_27B_UNCENSORED_ALIASES') == 1
    assert '"id_prefix": "dgx2-qwen36-27b-uncensored-nvfp4"' in text
    assert '"backend": "dgx1"' in _sync_block(
        text,
        '"name": "qwen36-27b-uncensored-dgx2"',
        '"name": "deepseek-v4-flash-tp2"',
    )

    # Los backends de los modelos borrados no deben volver.
    for dead in ("QWEN36_27B_DENSE_ALIASES", "QWEN36_35B_DGX1_ALIASES",
                 "QWEN36_35B_DGX2_ALIASES", "GEMMA_ALIASES"):
        assert dead not in text, f"{dead} apunta a un deployment borrado"


def test_dense_uncensored_is_never_auto_routed():
    """El nombre explicito `dense-uncensored` no lo reescribe el router: una
    llamada directa debe fallar si DGX2 esta caido, no responder desde Ornith."""
    text = MANIFEST.read_text()
    routed = _sync_block(text, "AUTO_ROUTED_MODELS = ", "# All four routes")
    assert "dense-uncensored" not in routed
    route = _sync_block(text, "ROUTE = {", "# Deterministic hints")
    assert "dense" not in route


def test_openclaw_team_permission_is_reconciled_without_removing_models():
    text = MANIFEST.read_text()
    assert 'OPENCLAW_TEAM_ID, value: "openclaw"' in text
    assert 'OPENCLAW_KEY_ALIAS, value: "openclaw-qwen36-prod"' in text
    # La lista crece (union aditiva), asi que se comprueba pertenencia y no el
    # literal: lo que importa aqui es que `dense-uncensored` siga concedido.
    match = re.search(r'OPENCLAW_TEAM_REQUIRED_MODELS, value: "([^"]*)"', text)
    assert match, "el manifest ya no declara OPENCLAW_TEAM_REQUIRED_MODELS"
    required = {name.strip() for name in match.group(1).split(",") if name.strip()}
    assert "dense-uncensored" in required
    # v024-f2-dgx1 fuera: su deployment se borro el 2026-07-26.
    assert "qwen36-27b-nvfp4-v024-f2-dgx1" not in required
    block = text[
        text.index("def reconcile_required_team_models"):
        text.index("def managed_model_id")
    ]
    assert 'f"{LITELLM_BASE_URL}/team/info?{query}"' in block
    assert 'f"{LITELLM_BASE_URL}/team/update"' in block
    assert 'desired = list(current)' in block
    assert 'payload={"team_id": OPENCLAW_TEAM_ID, "models": desired}' in block
    assert 'if key.get("key_alias") != OPENCLAW_KEY_ALIAS:' in block
    assert 'f"{LITELLM_BASE_URL}/key/update"' in block
    assert 'payload={"key": key_token, "models": key_desired}' in block
    assert "/team/new" not in block


def test_backends_cubren_las_formas_de_exclusion():
    """2 backends declarados, y la exclusion tiene dos formas distintas.

    Actualizado 2026-08-13 (ventana RHO backend-sync): eran 4. Se retiraron
    `ornith-dgx1` y `nvidia-qwen36-dgx1`, los dos candidatos al asiento de DGX1:
    estaban a replicas 0 en el overlay Y SIN PESOS EN DISCO (Ornith borrado el
    10-08; la carpeta nvidia-qwen36-35b-a3b-nvfp4 no existe en dgx1), asi que
    ninguno podia arrancar. El asiento en si caduco el 08-08, cuando DeepSeek TP=2
    paso a ocupar los DOS Sparks: mientras corre no cabe residente en DGX1, no por
    politica sino por memoria.

    (Nota historica que sigue valiendo: este test estuvo ROJO desde el 10-08 sin
    que nadie lo viera, porque CI solo corre el contrato de red.)

    DGX2: desde el 2026-08-10 solo queda el 27B denso. Habia co-residencia (2
    replicas de GPU por time-slicing) con Qwen3-Coder, que se retiro del cluster
    entero. La regla sigue en pie para quien meta otro co-residente: no comparte nodo
    con DGX1, asi que no hay exclusion fisica y NO puede declarar alias de tooling.

    LOS DOS A LA VEZ: DeepSeek-V4-Flash en TP=2 pide ~104 GiB de CADA Spark, asi
    que su exclusion es fisica igual que la de DGX1 pero abarca los dos nodos. Por
    eso SI comparte los alias del residente de tooling: mientras corre no puede
    haber residente de DGX1 ni co-residente de DGX2.
    """
    text = MANIFEST.read_text()
    backends = _sync_block(text, "BACKENDS = (", "RETIRED_MANAGED_IDS")
    assert backends.count('"name": "') == 2
    for name in ("qwen36-27b-uncensored-dgx2", "deepseek-v4-flash-tp2"):
        assert f'"name": "{name}"' in backends
    # El de TP=2 tiene que decir que vive en los dos nodos: es lo que justifica que
    # comparta los alias de tooling sin romper la unicidad.
    ds = backends[backends.index('"name": "deepseek-v4-flash-tp2"'):]
    assert '"backend": "dgx1+dgx2"' in ds
    for dead in ("gemma-dgx1", "qwen36-35b-dgx1", "qwen36-35b-dgx2",
                 "qwen36-27b-dense-dgx1", "qwen36-27b-dense-dgx2",
                 # retirado del cluster entero el 2026-08-10
                 "qwen3coder-dgx2", "qwen3coder-dgx1",
                 # 2026-08-13: sin pesos en disco, no podian arrancar
                 "ornith-dgx1", "nvidia-qwen36-dgx1"):
        assert f'"name": "{dead}"' not in backends

    # El backend conserva su identidad estable, pero Creative lo sirve en DGX1
    # con la ventana real de 64K en vez de heredar 256K.
    dense = backends[backends.index('"name": "qwen36-27b-uncensored-dgx2"'):
                     backends.index('"name": "deepseek-v4-flash-tp2"')]
    assert '"backend": "dgx1"' in dense
    assert '"max_input_tokens": 65536' in dense
    assert '"supports_function_calling": True' in dense

    # Ninguno de los id_prefix nuevos puede caer bajo un prefijo retirado, que
    # cleanup_retired_models() purga en cada ciclo.
    retired = _sync_block(text, "RETIRED_MANAGED_ID_PREFIXES = (", "TOKEN_PATH =")
    dead_prefixes = re.findall(r'"(dgx\d-[a-z0-9-]+-)"', retired)
    for prefix in ("dgx1-nvidia-qwen36-35b-nvfp4-", "ds4-flash-0731-tp2-",
                   "dgx2-qwen36-27b-uncensored-nvfp4-"):
        for dead in dead_prefixes:
            assert not prefix.startswith(dead), f"{prefix} seria purgado por {dead}"
    # Y el del coder retirado SI tiene que estar purgado, para que no le sobreviva
    # ningun registro en el model_list.
    assert any("dgx2-qwen3coder-30b-a3b-nvfp4-".startswith(d) for d in dead_prefixes)


def test_shared_tooling_alias_is_guarded_against_double_registration():
    """LiteLLM no impone unicidad de alias: si dos backends de DGX1 quedaran
    registrados a la vez sobre `tooling`, el router balancearia contra un
    api_base muerto. La exclusion por hardware NO es una invariante de este
    controlador (endpoint_ready devuelve None y se SALTA el backend), asi que
    habilitar un backend debe expulsar explicitamente a sus hermanos."""
    text = MANIFEST.read_text()
    # 2026-08-13: ya solo hay UN backend con los nombres de tooling. Los dos
    # candidatos de DGX1 (ornith-dgx1 via ORNITH_ALIASES, nvidia-qwen36-dgx1 via
    # TOOLING_RESIDENT_ALIASES) se retiraron por no tener pesos en disco, y con
    # ellos desaparecio ORNITH_ALIASES. La invariante que protege este test NO es
    # el numero de candidatos: es que TODO el que declare esos alias excluya a los
    # demas por hardware. Con uno solo se cumple trivialmente, y el assert de
    # abajo es lo que impide que se anada un segundo sin exclusion fisica.
    assert "TOOLING_RESIDENT_ALIASES = QWEN36_COMPAT_ALIASES" in text
    assert "ORNITH_ALIASES" not in _sync_block(text, "BACKENDS = (", "RETIRED_MANAGED_IDS"), (
        "ORNITH_ALIASES volvio a BACKENDS: sus dos duenos estan retirados"
    )

    backends = _sync_block(text, "BACKENDS = (", "RETIRED_MANAGED_IDS")
    comparten = [n for n, a in re.findall(r'"name":\s*"([a-z0-9-]+)".*?"aliases":\s*(\w+)',
                                          backends, re.S)
                 if a in ("ORNITH_ALIASES", "TOOLING_RESIDENT_ALIASES")]
    # 2026-08-13: UNO. Eran tres (los dos candidatos de DGX1 + DeepSeek); los de
    # DGX1 se retiraron sin pesos en disco. DeepSeek se queda los nombres porque su
    # TP=2 ocupa los DOS Sparks, o sea que su exclusion fisica abarca el cluster
    # entero. Si algun dia se anade aqui un backend que NO excluya a los otros por
    # hardware, `tooling` acabaria servido por dos api_base a la vez y el router
    # balancearia contra uno muerto: eso es lo que protege este test, no el numero.
    assert sorted(comparten) == ["deepseek-v4-flash-tp2"], comparten

    # El backend de DGX2 NO puede compartirlos: no hay exclusion fisica entre nodos
    # distintos, asi que `tooling` acabaria servido por dos backends.
    dense = backends[backends.index('"name": "qwen36-27b-uncensored-dgx2"'):
                     backends.index('"name": "deepseek-v4-flash-tp2"')]
    assert '"aliases": QWEN36_27B_UNCENSORED_ALIASES' in dense
    # Comprobar la DECLARACION, no cualquier mencion.
    assert '"aliases": TOOLING_RESIDENT_ALIASES' not in dense
    # Y los alias del coder retirado no vuelven por la puerta de atras.
    assert "QWEN3CODER_ALIASES" not in text

    guard = _sync_block(text, "def conflicting_managed_ids", "def reconcile_backend")
    assert 'own_aliases & set(other["aliases"])' in guard
    assert "managed_model_id(other, alias)" in guard

    reconcile = _sync_block(text, "def reconcile_backend", "def main()")
    # La expulsion ocurre ANTES del alta, no despues.
    assert reconcile.index("conflicting_managed_ids(backend)") < reconcile.index("add_model(deployment)")


def test_manifest_stays_valid_yaml():
    list(yaml.safe_load_all(MANIFEST.read_text()))
