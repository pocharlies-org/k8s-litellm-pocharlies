"""DGX-454 (C7 de DGX-451): el default de `bot_keys` de la valvula KV.

Tras el parto de Hermes, `hermes` son chats interactivos y el fondo de la
compania entra con la clave `hermes-batch` (o con la clase `company`). Penalizar
a `hermes` con el umbral bajo bot_budget_pct contradice la epica. Contrato:
dgx.model-routing.config.v2 en CONTRACTS.yaml.
"""
import sys
import types
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


@pytest.fixture(scope="module")
def router_mod():
    """session_router.py del ConfigMap litellm-config, con httpx stubbeado."""
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    cm = next(d for d in docs if d.get("kind") == "ConfigMap"
              and d["metadata"]["name"] == "litellm-config")
    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = types.ModuleType("httpx")
    try:
        module = types.ModuleType("session_router_bot_keys")
        exec(compile(cm["data"]["session_router.py"], "session_router.py", "exec"),
             module.__dict__)
    finally:
        if saved is not None:
            sys.modules["httpx"] = saved
        else:
            sys.modules.pop("httpx", None)
    return module


def test_bot_keys_por_defecto_son_hermes_batch_aurora_rca_y_brain(router_mod):
    assert tuple(router_mod.DEFAULT_BOT_KEYS) == ("hermes-batch", "aurora-rca", "brain")
    bots = router_mod._sanitize({})["bot_keys"]
    assert "hermes-batch" in bots and "aurora-rca" in bots and "brain" in bots
    assert "hermes" not in bots


def test_is_bot_session_alias_hermes_batch_sin_clase(router_mod):
    cfg = router_mod._sanitize({})
    assert router_mod._is_bot_session(cfg, None, "hermes-batch") is True


def test_is_bot_session_alias_hermes_sin_clase_no_es_bot(router_mod):
    cfg = router_mod._sanitize({})
    assert router_mod._is_bot_session(cfg, None, "hermes") is False


def test_is_bot_session_config_vieja_cae_al_default_nuevo(router_mod):
    assert router_mod._is_bot_session({}, None, "hermes-batch") is True
    assert router_mod._is_bot_session({}, None, "hermes") is False
