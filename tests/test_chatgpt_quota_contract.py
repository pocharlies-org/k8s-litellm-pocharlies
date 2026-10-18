"""dgx.litellm.chatgpt-quota.v1: la cuota de la cuenta ChatGPT sale de las cabeceras x-codex-* (DGX-1078).

El dashboard pinta la ventana de 5 h y la semanal de la cuenta ChatGPT Plus a partir de la
ultima lectura que este callback deja en litellm-valkey. Aqui se fija la forma de esa lectura
(la lee otro repo), que un 429 tambien cuenta y que el estado opaco `turn-state` no se guarda.
"""
import asyncio
import json
import sys
import types

from manifest_docs import config_data, named

# Las cabeceras tal cual las devolvio el backend de Codex el 18-10-2026 (gpt-6-luna).
HEADERS = {
    "x-codex-active-limit": "premium",
    "x-codex-credits-balance": "0",
    "x-codex-credits-has-credits": "False",
    "x-codex-credits-unlimited": "False",
    "x-codex-plan-type": "plus",
    "x-codex-primary-reset-at": "1792333183",
    "x-codex-primary-used-percent": "12.5",
    "x-codex-primary-window-minutes": "300",
    "x-codex-secondary-reset-at": "1792885372",
    "x-codex-secondary-used-percent": "3",
    "x-codex-secondary-window-minutes": "10080",
    "x-codex-turn-state": "gAAAAAopaque",
}


class _Valkey:
    def __init__(self):
        self.sets = []

    async def set(self, key, value, ex=None):
        self.sets.append((key, json.loads(value), ex))


def _module(valkey):
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = object
    router = types.ModuleType("session_router")

    async def _redis():
        return valkey

    router._redis = _redis
    stubs = {"litellm": types.ModuleType("litellm"),
             "litellm.integrations": types.ModuleType("litellm.integrations"),
             "litellm.integrations.custom_logger": custom_logger,
             "session_router": router}
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        mod = types.ModuleType("chatgpt_quota")
        exec(compile(config_data()["chatgpt_quota.py"], "<chatgpt_quota>", "exec"), mod.__dict__)  # noqa: S102
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_la_lectura_tiene_las_dos_ventanas_y_nada_opaco():
    mod = _module(_Valkey())
    reading = mod.quota_from_headers(mod.codex_headers(HEADERS), now=1792316176.0)
    assert reading == {
        "observed_at": 1792316176.0,
        "plan_type": "plus",
        "active_limit": "premium",
        "primary": {"used_percent": 12.5, "window_minutes": 300, "reset_at": 1792333183},
        "secondary": {"used_percent": 3.0, "window_minutes": 10080, "reset_at": 1792885372},
        "credits_unlimited": False,
        "credits_balance": 0.0,
    }
    assert "turn" not in json.dumps(reading)


def test_exito_con_prefijo_de_litellm_se_guarda_en_valkey():
    valkey = _Valkey()
    mod = _module(valkey)
    resp = types.SimpleNamespace(_hidden_params={
        "additional_headers": {"llm_provider-" + k: v for k, v in HEADERS.items()}})
    asyncio.run(mod.proxy_handler_instance.async_log_success_event({}, resp, None, None))
    [(key, reading, ttl)] = valkey.sets
    assert key == "chatgpt:quota" == mod.CHATGPT_QUOTA_KEY
    assert ttl == 8 * 86400
    assert reading["primary"]["used_percent"] == 12.5


def test_un_429_tambien_cuenta():
    valkey = _Valkey()
    mod = _module(valkey)
    exc = Exception("rate limited")
    exc.litellm_response_headers = dict(HEADERS, **{"x-codex-primary-used-percent": "100"})
    asyncio.run(mod.proxy_handler_instance.async_log_failure_event({"exception": exc}, None, None, None))
    assert valkey.sets[0][1]["primary"]["used_percent"] == 100.0


def test_otro_proveedor_no_escribe():
    valkey = _Valkey()
    mod = _module(valkey)
    resp = types.SimpleNamespace(_hidden_params={"additional_headers": {"llm_provider-x-ratelimit-remaining-tokens": "9"}})
    asyncio.run(mod.proxy_handler_instance.async_log_success_event({"response_headers": None}, resp, None, None))
    assert valkey.sets == []


def test_valkey_caido_no_rompe():
    class Roto:
        async def set(self, *a, **k):
            raise ConnectionError("down")

    mod = _module(Roto())
    asyncio.run(mod.proxy_handler_instance.async_log_success_event({"response_headers": HEADERS}, None, None, None))


def test_cableado_en_el_proxy():
    import yaml
    cfg = yaml.safe_load(config_data()["config.yaml"])
    assert "chatgpt_quota.proxy_handler_instance" in cfg["litellm_settings"]["callbacks"]
    pod = named("Deployment", "litellm")["spec"]["template"]["spec"]
    litellm = next(c for c in pod["containers"] if c["name"] == "litellm")
    assert {"name": "config", "mountPath": "/app/chatgpt_quota.py", "subPath": "chatgpt_quota.py",
            "readOnly": True} in litellm["volumeMounts"]
