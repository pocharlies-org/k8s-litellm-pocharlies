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

    # Los backends de los modelos borrados no deben volver.
    for dead in ("QWEN36_27B_DENSE_ALIASES", "QWEN36_35B_DGX1_ALIASES",
                 "QWEN36_35B_DGX2_ALIASES", "GEMMA_ALIASES"):
        assert dead not in text, f"{dead} apunta a un deployment borrado"


def test_dense_uncensored_is_never_auto_routed():
    """El nombre explicito `dense-uncensored` no lo reescribe el router: una
    llamada directa debe fallar si DGX2 esta caido, no responder desde Ornith."""
    text = MANIFEST.read_text()
    routed = _sync_block(text, "AUTO_ROUTED_MODELS = ", "# Where each shape goes")
    assert "dense-uncensored" not in routed
    route = _sync_block(text, "ROUTE = {", "# Hard limit escape")
    assert "dense-uncensored" not in route


def test_openclaw_team_permission_is_reconciled_without_removing_models():
    text = MANIFEST.read_text()
    assert 'OPENCLAW_TEAM_ID, value: "openclaw"' in text
    assert 'OPENCLAW_KEY_ALIAS, value: "openclaw-qwen36-prod"' in text
    # v024-f2-dgx1 fuera: su deployment se borro el 2026-07-26.
    assert (
        'OPENCLAW_TEAM_REQUIRED_MODELS, value: '
        '"ornith-canary,ornith-1.0,dense-uncensored,router"'
    ) in text
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


def test_backends_are_one_dgx2_dense_plus_three_exclusive_dgx1_candidates():
    """4 backends declarados, 2 registrables a la vez (2026-08-03).

    Los tres de DGX1 son candidatos MUTUAMENTE EXCLUYENTES al mismo asiento: una
    sola GPU con sharing-strategy=none, todos piden `nvidia.com/gpu: 1` a
    replicas=1, asi que como mucho uno puede estar Ready. Por eso comparten el
    mismo tuple de alias.
    """
    text = MANIFEST.read_text()
    backends = _sync_block(text, "BACKENDS = (", "RETIRED_MANAGED_IDS")
    assert backends.count('"name": "') == 4
    for name in ("ornith-dgx1", "qwen3coder-dgx1", "nvidia-qwen36-dgx1",
                 "qwen36-27b-uncensored-dgx2"):
        assert f'"name": "{name}"' in backends
    for dead in ("gemma-dgx1", "qwen36-35b-dgx1", "qwen36-35b-dgx2",
                 "qwen36-27b-dense-dgx1", "qwen36-27b-dense-dgx2"):
        assert f'"name": "{dead}"' not in backends

    # Solo el de NVIDIA es multimodal entre los candidatos nuevos; Qwen3-Coder es
    # solo texto y debe declararlo, no heredar el True de Ornith.
    coder = backends[backends.index('"name": "qwen3coder-dgx1"'):
                     backends.index('"name": "nvidia-qwen36-dgx1"')]
    assert '"supports_vision": False' in coder
    assert '"context_window": 262144' in coder
    assert '"supports_function_calling": True' in coder

    # Ninguno de los id_prefix nuevos puede caer bajo un prefijo retirado, que
    # cleanup_retired_models() purga en cada ciclo.
    retired = _sync_block(text, "RETIRED_MANAGED_ID_PREFIXES = (", "TOKEN_PATH =")
    dead_prefixes = re.findall(r'"(dgx\d-[a-z0-9-]+-)"', retired)
    for prefix in ("dgx1-qwen3coder-30b-a3b-nvfp4-", "dgx1-nvidia-qwen36-35b-nvfp4-"):
        for dead in dead_prefixes:
            assert not prefix.startswith(dead), f"{prefix} seria purgado por {dead}"


def test_shared_tooling_alias_is_guarded_against_double_registration():
    """LiteLLM no impone unicidad de alias: si dos backends de DGX1 quedaran
    registrados a la vez sobre `tooling`, el router balancearia contra un
    api_base muerto. La exclusion por hardware NO es una invariante de este
    controlador (endpoint_ready devuelve None y se SALTA el backend), asi que
    habilitar un backend debe expulsar explicitamente a sus hermanos."""
    text = MANIFEST.read_text()
    assert "TOOLING_RESIDENT_ALIASES = QWEN36_COMPAT_ALIASES" in text
    assert text.count('"aliases": TOOLING_RESIDENT_ALIASES') == 2

    guard = _sync_block(text, "def conflicting_managed_ids", "def reconcile_backend")
    assert 'own_aliases & set(other["aliases"])' in guard
    assert "managed_model_id(other, alias)" in guard

    reconcile = _sync_block(text, "def reconcile_backend", "def main()")
    # La expulsion ocurre ANTES del alta, no despues.
    assert reconcile.index("conflicting_managed_ids(backend)") < reconcile.index("add_model(deployment)")


def test_manifest_stays_valid_yaml():
    list(yaml.safe_load_all(MANIFEST.read_text()))
