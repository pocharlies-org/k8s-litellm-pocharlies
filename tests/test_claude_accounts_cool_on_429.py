"""DGX-1649: las 8 cuentas `claude-acct-*` aparcan al recibir un 429.

Un 429 de Anthropic llega SIN cabeceras `anthropic-ratelimit-unified-*` (medido el
2026-11-16: las 8 cuentas, dos organizaciones, con `x-should-retry: true`), asi que
`session_router.claude_account_filter` no puede actualizar la lectura de cuota ni
marcar la cuenta como rechazada. Lo unico que la saca de la rotacion es el cooldown
del Router, que en el camino del fallo se toma de `model_info` (no de
`litellm_params`): si alguna de las 8 vuelve a `cooldown_time: 0`, el filtro repite
la cuenta estrangulada y las sesiones se quedan esperando.
"""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"


def _deployments():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            cfg = yaml.safe_load(doc["data"]["config.yaml"])
            return [d for d in cfg["model_list"]
                    if str(d.get("model_info", {}).get("id", "")).startswith("claude-acct-")]
    raise AssertionError("no encuentro el ConfigMap litellm-config")


def test_las_ocho_cuentas_claude_aparcan_al_recibir_un_429():
    deps = _deployments()
    assert len(deps) == 8, f"se esperaban 8 cuentas claude, hay {len(deps)}"
    for dep in deps:
        info = dep["model_info"]
        cooldown = info.get("cooldown_time")
        assert isinstance(cooldown, int) and cooldown > 0, f"{info['id']} sin cooldown_time positivo"
        # el `allowed_fails: 3` del router_settings cuenta por cuenta y minuto, y sobre 8
        # cuentas con simple-shuffle no se alcanza: sin esto no aparca nunca
        assert info.get("allowed_fails") == 0, f"{info['id']} sin allowed_fails: 0"
        # el camino del fallo lee model_info, no litellm_params: ahí no vale
        assert "cooldown_time" not in (dep["litellm_params"].get("headers") or {}), info["id"]