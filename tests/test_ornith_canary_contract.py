from pathlib import Path


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def test_ornith_is_one_readiness_gated_backend_owning_the_tooling_routes():
    """Ornith es el UNICO backend de DGX1 y dueño de tooling/router/auto y de los
    alias de compatibilidad qwen36-35b*, mas sus dos nombres de canary.

    NOTA: este test estaba ROJO antes del 2026-07-27 por dos asserts rancios --
    esperaba `"aliases": ORNITH_CANARY_ALIASES` (el backend usa ORNITH_ALIASES,
    que es CANARY + COMPAT) y `"context_window": 65536` (son 262144). Se corrige
    aqui junto con el resto del saneamiento.
    """
    text = MANIFEST.read_text()

    assert "ORNITH_CANARY_ALIASES = (" in text
    alias_block = text[
        text.index("ORNITH_CANARY_ALIASES = ("):
        text.index("QWEN36_COMPAT_ALIASES = (")
    ]
    assert '"ornith-canary"' in alias_block
    assert '"ornith-1.0"' in alias_block
    # Los nombres de canary no deben colarse en el bloque de compatibilidad.
    assert '"tooling"' not in alias_block

    # El backend usa la union (canary + compat) exactamente una vez.
    assert text.count('"aliases": ORNITH_ALIASES') == 1
    assert "ORNITH_ALIASES = ORNITH_CANARY_ALIASES + QWEN36_COMPAT_ALIASES" in text
    assert '"id_prefix": "dgx1-ornith-35b-nvfp4-mtp"' in text
    assert '"context_window": 262144' in text
    assert '"max_parallel_requests": 4' in text

    # `tooling` y compañia SI los sirve Ornith...
    compat = text[
        text.index("QWEN36_COMPAT_ALIASES = ("):
        text.index("ORNITH_ALIASES =")
    ]
    for name in ('"tooling"', '"router"', '"auto"', '"litellmrouter"'):
        assert name in compat

    # ...pero los nombres de canary NO son auto-enrutados por el hook.
    routed = text[text.index("AUTO_ROUTED_MODELS = "):text.index("# Where each shape goes")]
    route = text[text.index("ROUTE = {"):text.index("# Hard limit escape")]
    for block in (routed, route):
        assert "ornith-canary" not in block
        assert "ornith-1.0" not in block


def test_ornith_model_metadata_uses_backend_specific_context():
    text = MANIFEST.read_text()
    desired = text[
        text.index("def desired_deployments"):
        text.index("def add_model")
    ]
    assert 'backend.get("max_tokens", 32768)' in desired
    assert 'backend.get("context_window", 262144)' in desired
