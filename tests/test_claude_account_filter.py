"""DGX-1099: la cuenta de Claude la elige LiteLLM con la politica por key del panel.

Unitarios de `session_router.claude_account_filter` y `claude_quota_from_headers`
contra una Valkey de pega, con el modulo tal cual lo embebe el ConfigMap.
"""
import asyncio
import json
import time
from pathlib import Path

import yaml

from test_alibaba_account_affinity import _pod as _base_pod  # harness compartido a proposito

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
DOCS = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
CM = next(d for d in DOCS if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
POLICY = {
    "claude-local": [{"id": "info", "salto_7d": 90, "salto_5h": 90},
                     {"id": "correo", "salto_7d": 90, "salto_5h": 90}],
    "claude-cloudblue": [{"id": "flash", "salto_7d": 90, "salto_5h": 90}],
}


class FakeValkey:
    def __init__(self):
        self.data = {}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("valkey caida")

    async def get(self, key):
        self._check()
        return self.data.get(key)

    async def mget(self, keys):
        self._check()
        return [self.data.get(k) for k in keys]

    async def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def expire(self, key, seconds):
        self._check()
        return key in self.data


def _pod(valkey, policy=POLICY):
    mod = _base_pod(valkey)

    async def fake_refresh():
        return None

    mod._refresh_config = fake_refresh
    mod._config_cache["config"] = mod._sanitize({"claude_accounts": policy})
    mod._config_cache["expires"] = time.monotonic() + 3600
    return mod


ACCOUNTS = ("info", "correo", "edani", "flash")


def _deps(accounts=ACCOUNTS):
    return [{"model_name": "claude-*", "model_info": {"id": f"claude-acct-{a}"},
             "litellm_params": {"model": "anthropic/claude-*"}} for a in accounts]


def _kw(alias="claude-local", sid="s1"):
    md = {"user_api_key_alias": alias}
    if sid:
        md["session_id"] = sid
    return {"metadata": md}


def _pick(mod, deps=None, **kw):
    out = asyncio.run(mod.claude_account_filter("claude-opus-5-5", deps or _deps(), _kw(**kw)))
    return [d["model_info"]["id"].removeprefix("claude-acct-") for d in out]


def _quota(v, acct, u5=0.1, u7=0.1, status="allowed", reset_in=3600, overage=False):
    now = time.time()
    v.data[f"claude:quota:{acct}"] = json.dumps({
        "observed_at": now, "status": status, "reset": int(now + reset_in),
        "five_hour": {"utilization": u5, "resets_at": int(now + reset_in)},
        "seven_day": {"utilization": u7, "resets_at": int(now + 86400)},
        "overage_in_use": overage})


def test_conversacion_nueva_va_a_la_primera_de_su_key():
    assert _pick(_pod(FakeValkey())) == ["info"]


def test_salta_a_la_siguiente_cuando_la_primera_pasa_su_salto():
    v = FakeValkey()
    _quota(v, "info", u5=0.95)
    assert _pick(_pod(v), sid="nueva") == ["correo"]


def test_conversacion_empezada_se_queda_por_debajo_de_su_salto():
    v = FakeValkey()
    mod = _pod(v)
    assert _pick(mod, sid="s1") == ["info"]
    _quota(v, "info", u5=0.89)
    assert _pick(mod, sid="s1") == ["info"]


def test_conversacion_empezada_salta_al_pasar_el_salto_de_su_cuenta():
    """Dani, 19-10: una abierta salta en el % de SU cuenta (90 o 50), no solo al 429."""
    v = FakeValkey()
    mod = _pod(v, policy={"claude-local": [{"id": "info", "salto_7d": 50, "salto_5h": 50},
                                           {"id": "correo", "salto_7d": 90, "salto_5h": 90}]})
    assert _pick(mod, sid="s1") == ["info"]
    _quota(v, "info", u7=0.49)
    assert _pick(mod, sid="s1") == ["info"]
    _quota(v, "info", u7=0.5)
    assert _pick(mod, sid="s1") == ["correo"]
    assert v.data["claude_account_pin:v1:claude-local:s1"] == "correo"


def test_conversacion_empezada_muda_si_su_cuenta_esta_rechazada():
    v = FakeValkey()
    mod = _pod(v)
    assert _pick(mod, sid="s1") == ["info"]
    _quota(v, "info", u5=1.0, status="rejected")
    assert _pick(mod, sid="s1") == ["correo"]
    assert v.data["claude_account_pin:v1:claude-local:s1"] == "correo"


def test_creditos_cuentan_como_agotada():
    v = FakeValkey()
    _quota(v, "info", overage=True)
    assert _pick(_pod(v)) == ["correo"]


def test_ventana_reiniciada_ya_no_cuenta():
    v = FakeValkey()
    _quota(v, "info", u5=1.0, status="rejected", reset_in=-10)
    assert _pick(_pod(v)) == ["info"]


def test_cuenta_en_cooldown_no_se_elige():
    assert _pick(_pod(FakeValkey()), deps=_deps(("correo", "edani"))) == ["correo"]


def test_nunca_sale_de_la_lista_de_su_key():
    v = FakeValkey()
    _quota(v, "flash", u7=0.99)
    assert _pick(_pod(v), alias="claude-cloudblue") == []
    assert _pick(_pod(v), alias="sin-politica") == []


def test_todas_pasadas_de_salto_es_lista_vacia():
    v = FakeValkey()
    _quota(v, "info", u5=0.95)
    _quota(v, "correo", u7=0.95)
    assert _pick(_pod(v)) == []


def test_valkey_caida_usa_la_primera():
    v = FakeValkey()
    v.down = True
    assert _pick(_pod(v)) == ["info"]


def test_otros_grupos_pasan_intactos():
    mod = _pod(FakeValkey())
    deps = [{"model_info": {"id": "alibaba-q38-max-k1"}}]
    assert asyncio.run(mod.claude_account_filter("alibaba-q38-max", deps, _kw())) == deps


def test_lectura_de_cabeceras_de_anthropic():
    mod = _pod(FakeValkey())
    h = {"llm_provider-anthropic-ratelimit-unified-5h-utilization": "0.91",
         "llm_provider-anthropic-ratelimit-unified-5h-reset": "1792362000",
         "llm_provider-anthropic-ratelimit-unified-7d-utilization": "0.29",
         "llm_provider-anthropic-ratelimit-unified-7d-reset": "1792908000",
         "llm_provider-anthropic-ratelimit-unified-status": "allowed_warning",
         "llm_provider-anthropic-ratelimit-unified-overage-status": "rejected",
         "content-type": "application/json"}
    r = mod.claude_quota_from_headers(h, now=1.0)
    assert r["five_hour"] == {"utilization": 0.91, "resets_at": 1792362000}
    assert r["seven_day"]["utilization"] == 0.29
    assert r["status"] == "allowed_warning" and r["overage_in_use"] is False
    assert mod.claude_quota_from_headers({"content-type": "x"}) is None


def test_el_callback_guarda_la_cuota_de_un_stream_de_messages():
    """Claude Code pide /v1/messages en streaming: ahi litellm no rellena _hidden_params
    y las cabeceras solo viajan en kwargs["httpx_response"] (medido en v1.105.1)."""
    import sys
    import types

    valkey = FakeValkey()
    router = _pod(valkey)
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = object
    stubs = {"litellm": types.ModuleType("litellm"),
             "litellm.integrations": types.ModuleType("litellm.integrations"),
             "litellm.integrations.custom_logger": custom_logger,
             "session_router": router}
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        cq = types.ModuleType("claude_quota")
        exec(compile(CM["data"]["claude_quota.py"], "<claude_quota>", "exec"), cq.__dict__)  # noqa: S102
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    httpx_response = types.SimpleNamespace(headers={
        "anthropic-ratelimit-unified-5h-utilization": "0.4",
        "anthropic-ratelimit-unified-7d-utilization": "0.2"})
    kwargs = {"litellm_params": {"model_info": {"id": "claude-acct-info"}},
              "standard_logging_object": {"hidden_params": {"additional_headers": None}},
              "httpx_response": httpx_response}
    asyncio.run(cq.proxy_handler_instance.async_log_success_event(kwargs, object(), 0, 0))
    reading = json.loads(valkey.data["claude:quota:info"])
    assert reading["five_hour"]["utilization"] == 0.4


def test_politica_basura_se_descarta():
    mod = _pod(FakeValkey())
    clean = mod._sanitize_claude_accounts({
        "Claude-Local": [{"id": "info", "salto_7d": 150}, {"id": "info"}, {"id": "../x"}, "x"],
        "otra": "no-lista"})
    assert clean == {"claude-local": [{"id": "info", "salto_7d": 90.0, "salto_5h": 90.0}]}


def test_las_entradas_claude_son_una_por_cuenta_sin_fallbacks():
    cfg = yaml.safe_load(CM["data"]["config.yaml"])
    entries = [m for m in cfg["model_list"] if m["model_name"] == "claude-*"]
    ids = [m["model_info"]["id"] for m in entries]
    assert len(ids) == len(set(ids)) >= 1 and all(i.startswith("claude-acct-") for i in ids)
    assert all(m["litellm_params"]["api_key"].startswith("os.environ/CLAUDE_OAT_") for m in entries)
    for fb in cfg["router_settings"].get("fallbacks") or ():
        assert not any(str(k).startswith("claude-") for k in fb), fb
    eso = next(d for d in DOCS if d.get("kind") == "ExternalSecret"
               and d["metadata"]["name"] == "litellm-claude-cuentas")
    secret_keys = {x["secretKey"] for x in eso["spec"]["data"]}
    assert {m["litellm_params"]["api_key"].removeprefix("os.environ/") for m in entries} == secret_keys


def test_historial_mixto_sin_campos_que_anthropic_rechaza():
    mod = _pod(FakeValkey())
    data = {"model": "claude-opus-5-5", "messages": [
        {"role": "assistant", "provider_specific_fields": {"y": 2},
         "content": [{"type": "text", "text": "hola", "provider_specific_fields": {"x": 1}}]}]}
    mod.stamp_claude_request(data)
    assert "provider_specific_fields" not in json.dumps(data["messages"])
    assert data["turn_off_message_logging"] is True
    otro = {"model": "qwen38-flash-next", "messages": [{"provider_specific_fields": 1}]}
    mod.stamp_claude_request(otro)
    assert otro["messages"] == [{"provider_specific_fields": 1}]


# ── Reglas de Claude de la compañía (DGX-1099 opción B: antes en el claude-router del x86) ──

GATE = {"mode": "rewrite", "allow": ["claude-opus-5-5"], "target": "qwen38-flash-next", "max_tokens": 32000}


def _company_pod(claude=True, gate=GATE, policy=None):
    mod = _pod(FakeValkey())
    raw = {"company": {"claude": claude, "claude_gate": gate},
           "claude_accounts": policy if policy is not None else {"compania": [{"id": "edani"}]}}
    mod._config_cache["config"] = mod._sanitize(raw)
    return mod


def _company_data(model, max_tokens=64000):
    return {"model": model, "max_tokens": max_tokens,
            "litellm_metadata": {"headers": {"X-Claude-Class": "company"},
                                 "user_api_key_alias": "claude-local"}}


def test_compania_con_claude_apagado_va_al_target_del_gate_o_recibe_403():
    # SC-2667: con rewrite, incluso lo permitido va al target (un rol del plugin pide su model: de git).
    mod = _company_pod(claude=False)
    data = _company_data("claude-opus-5-5")
    assert asyncio.run(mod.apply_company_policy(data, "claude-opus-5-5")) == (False, None)
    assert data["model"] == "qwen38-flash-next" and data["max_tokens"] == 32000
    for modo in ("off", "block"):
        mod = _company_pod(claude=False, gate={**GATE, "mode": modo})
        denied, detail = asyncio.run(mod.apply_company_policy(_company_data("claude-opus-5-5"), "claude-opus-5-5"))
        assert denied and detail["error"] == "company_claude_disabled", modo


def test_gate_rewrite_lleva_al_target_con_tope_y_deja_pasar_lo_permitido():
    mod = _company_pod()
    data = _company_data("claude-sonnet-5-5")
    assert asyncio.run(mod.apply_company_policy(data, "claude-sonnet-5-5")) == (False, None)
    assert data["model"] == "qwen38-flash-next" and data["max_tokens"] == 32000
    ok = _company_data("claude-opus-5-5")
    asyncio.run(mod.apply_company_policy(ok, "claude-opus-5-5"))
    assert ok["model"] == "claude-opus-5-5" and ok["max_tokens"] == 64000


def test_gate_block_es_403_y_sin_clase_no_se_toca():
    mod = _company_pod(gate={**GATE, "mode": "block"})
    denied, detail = asyncio.run(mod.apply_company_policy(_company_data("claude-haiku-5-5"), "claude-haiku-5-5"))
    assert denied and detail["error"] == "company_claude_gate"
    dani = {"model": "claude-haiku-5-5", "litellm_metadata": {"user_api_key_alias": "claude-local"}}
    assert asyncio.run(mod.apply_company_policy(dani, "claude-haiku-5-5")) == (False, None)
    assert dani["model"] == "claude-haiku-5-5"


def test_gate_con_target_claude_o_basura_queda_apagado():
    mod = _pod(FakeValkey())
    assert mod._sanitize_claude_gate({**GATE, "target": "claude-opus-5"})["mode"] == "off"
    assert mod._sanitize_claude_gate({"mode": "raro"})["mode"] == "off"
    assert mod._sanitize_claude_gate(None)["mode"] == "off"


def test_la_compania_elige_cuenta_de_su_lista_y_no_de_la_key():
    mod = _company_pod(policy={"compania": [{"id": "edani"}], "claude-local": [{"id": "info"}]})
    kw = _company_data("claude-opus-5-5")
    kw["litellm_metadata"]["session_id"] = "s-comp"
    out = asyncio.run(mod.claude_account_filter("claude-opus-5-5", _deps(), kw))
    assert [d["model_info"]["id"] for d in out] == ["claude-acct-edani"]


def test_plan_claude_de_la_compania_respeta_el_interruptor():
    on, off = _company_pod(), _company_pod(claude=False)
    data = _company_data("qwen38-flash-next")
    assert on._claude_plan_allowed(on._config_cache["config"], data)
    assert not off._claude_plan_allowed(off._config_cache["config"], data)
