"""DGX-1099: la cuenta de Claude la elige LiteLLM con la politica por key del panel.

Unitarios de `session_router.claude_account_filter` y `claude_quota_from_headers`
contra una Valkey de pega, con el modulo tal cual lo embebe el ConfigMap.
"""
import asyncio
import json
import logging
import sys
import time
import types
from pathlib import Path

import yaml

from test_alibaba_account_affinity import _pod as _base_pod  # harness compartido a proposito

MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
DOCS = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
CM = next(d for d in DOCS if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
POLICY = {
    "claude-x86": [{"id": "info", "salto_7d": 90, "salto_5h": 90},
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


def _kw(alias="claude-x86", sid="s1"):
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
    mod = _pod(v, policy={"claude-x86": [{"id": "info", "salto_7d": 50, "salto_5h": 50},
                                           {"id": "correo", "salto_7d": 90, "salto_5h": 90}]})
    _quota(v, "correo", u7=0.89)  # INFRA-1204: sin lecturas correo (salto 90) tendria mas margen que info (50)
    assert _pick(mod, sid="s1") == ["info"]
    _quota(v, "info", u7=0.49)
    assert _pick(mod, sid="s1") == ["info"]
    _quota(v, "info", u7=0.5)
    assert _pick(mod, sid="s1") == ["correo"]
    assert v.data["claude_account_pin:v1:claude-x86:s1"] == "correo"


def test_conversacion_empezada_muda_si_su_cuenta_esta_rechazada():
    v = FakeValkey()
    mod = _pod(v)
    assert _pick(mod, sid="s1") == ["info"]
    _quota(v, "info", u5=1.0, status="rejected")
    assert _pick(mod, sid="s1") == ["correo"]
    assert v.data["claude_account_pin:v1:claude-x86:s1"] == "correo"


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


class FakeValkeyLenta(FakeValkey):
    """Valkey vivo pero ocupado: cada comando contesta en 150 ms (DGX-1417 midio en
    el slowlog que el MGET de cooldown del propio litellm bloquea su hilo unico
    12-60 ms; con el presupuesto viejo de 100 ms eso era un fallo cada rafaga)."""

    async def mget(self, keys):
        await asyncio.sleep(0.15)
        return await FakeValkey.mget(self, keys)

    async def get(self, key):
        await asyncio.sleep(0.15)
        return await FakeValkey.get(self, key)

    async def set(self, key, value, nx=False, ex=None):
        await asyncio.sleep(0.15)
        return await FakeValkey.set(self, key, value, nx=nx, ex=ex)

    async def expire(self, key, seconds):
        await asyncio.sleep(0.15)
        return await FakeValkey.expire(self, key, seconds)


def test_valkey_lenta_no_es_error_ni_pierde_el_pin():
    """DGX-1417: el presupuesto del filtro es de 500 ms; una Valkey que tarda 150 ms
    no es Valkey caída y la conversacion conserva su cuenta."""
    mod = _pod(FakeValkeyLenta())
    assert _pick(mod) == ["info"]
    assert _pick(mod) == ["info"]  # segunda pasada: por pin, no por primera de la lista
    assert mod.CLAUDE_ACCOUNT_STATS["redis_errors"] == 0
    assert mod.CLAUDE_ACCOUNT_STATS["hits"] == 1


def test_sin_alias_no_cuenta_como_key_sin_cuentas():
    """DGX-1417: `empty` mide keys que se quedaron sin cuentas; una peticion sin
    alias (master key, sonda) se deniega igual pero no entra en el contador."""
    mod = _pod(FakeValkey())
    antes = mod.CLAUDE_ACCOUNT_STATS["empty"]
    assert asyncio.run(mod.claude_account_filter("claude-opus-5-5", _deps(), {})) == []
    assert mod.CLAUDE_ACCOUNT_STATS["empty"] == antes


def test_key_sin_politica_si_cuenta_empty():
    mod = _pod(FakeValkey())
    antes = mod.CLAUDE_ACCOUNT_STATS["empty"]
    assert _pick(mod, alias="otra-key") == []
    assert mod.CLAUDE_ACCOUNT_STATS["empty"] == antes + 1


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
    valkey = FakeValkey()
    cq = _claude_quota_module(valkey)
    httpx_response = types.SimpleNamespace(headers={
        "anthropic-ratelimit-unified-5h-utilization": "0.4",
        "anthropic-ratelimit-unified-7d-utilization": "0.2"})
    kwargs = {"litellm_params": {"model_info": {"id": "claude-acct-info"}},
              "standard_logging_object": {"hidden_params": {"additional_headers": None}},
              "httpx_response": httpx_response}
    asyncio.run(cq.proxy_handler_instance.async_log_success_event(kwargs, object(), 0, 0))
    reading = json.loads(valkey.data["claude:quota:info"])
    assert reading["five_hour"]["utilization"] == 0.4


def test_un_429_aparca_la_cuenta_y_el_filtro_la_salta():
    """DGX-1649: el Router no aparca los deployments del comodin `claude-*`; la marca la
    pone el callback, tambien cuando el 429 trae ventanas con margen (status allowed_warning)."""
    valkey = FakeValkey()
    mod = _pod(valkey)
    _quota(valkey, "info", u5=0.1)
    _quota(valkey, "correo", u5=0.5)  # sin el aparcado, info gana por margen
    cq = _claude_quota_module(valkey)
    # el 429 real: cabeceras unificadas con 16 % usado y aun asi rechaza
    httpx_response = types.SimpleNamespace(headers={
        "anthropic-ratelimit-unified-5h-utilization": "0.16",
        "anthropic-ratelimit-unified-status": "allowed_warning"})
    kwargs = {"litellm_params": {"model_info": {"id": "claude-acct-info"}},
              "httpx_response": httpx_response,
              "exception": types.SimpleNamespace(status_code=429)}
    asyncio.run(cq.proxy_handler_instance.async_log_failure_event(kwargs, None, 0, 0))
    reading = json.loads(valkey.data["claude:quota:info"])
    assert reading["cooldown_until"] > time.time()
    assert reading["five_hour"]["utilization"] == 0.16  # la ventana nueva del panel
    assert _pick(mod, sid="s1") == ["correo"]
    # sin cabeceras: se aparca igual y se conservan las ventanas que ya habia
    kwargs = {"litellm_params": {"model_info": {"id": "claude-acct-correo"}},
              "exception": types.SimpleNamespace(status_code=429)}
    asyncio.run(cq.proxy_handler_instance.async_log_failure_event(kwargs, None, 0, 0))
    reading = json.loads(valkey.data["claude:quota:correo"])
    assert reading["five_hour"]["utilization"] == 0.5
    assert reading["cooldown_until"] > time.time()
    # cumplido el minuto, info vuelve al sorteo
    for acct in ("info", "correo"):
        lectura = json.loads(valkey.data[f"claude:quota:{acct}"])
        lectura.pop("cooldown_until")
        valkey.data[f"claude:quota:{acct}"] = json.dumps(lectura)
    entrada = {"id": "info", "salto_7d": 90.0, "salto_5h": 90.0}
    assert not mod._claude_over_salto(json.loads(valkey.data["claude:quota:info"]), entrada, time.time())


def _claude_quota_module(valkey):
    """claude_quota.py tal cual lo embebe el ConfigMap, con litellm de pega."""
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
    return cq


def test_politica_basura_se_descarta():
    mod = _pod(FakeValkey())
    clean = mod._sanitize_claude_accounts({
        "Claude-X86": [{"id": "info", "salto_7d": 150}, {"id": "info"}, {"id": "../x"}, "x"],
        "otra": "no-lista"})
    assert clean == {"claude-x86": [{"id": "info", "salto_7d": 90.0, "salto_5h": 90.0,
                                      "salto_fable": 100.0}]}


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
                                 "user_api_key_alias": "claude-x86"}}


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
    dani = {"model": "claude-haiku-5-5", "litellm_metadata": {"user_api_key_alias": "claude-x86"}}
    assert asyncio.run(mod.apply_company_policy(dani, "claude-haiku-5-5")) == (False, None)
    assert dani["model"] == "claude-haiku-5-5"


def test_gate_con_target_claude_o_basura_queda_apagado():
    mod = _pod(FakeValkey())
    assert mod._sanitize_claude_gate({**GATE, "target": "claude-opus-5"})["mode"] == "off"
    assert mod._sanitize_claude_gate({"mode": "raro"})["mode"] == "off"
    assert mod._sanitize_claude_gate(None)["mode"] == "off"


def test_la_compania_elige_cuenta_de_su_lista_y_no_de_la_key():
    mod = _company_pod(policy={"compania": [{"id": "edani"}], "claude-x86": [{"id": "info"}]})
    kw = _company_data("claude-opus-5-5")
    kw["litellm_metadata"]["session_id"] = "s-comp"
    out = asyncio.run(mod.claude_account_filter("claude-opus-5-5", _deps(), kw))
    assert [d["model_info"]["id"] for d in out] == ["claude-acct-edani"]


def test_plan_claude_de_la_compania_respeta_el_interruptor():
    on, off = _company_pod(), _company_pod(claude=False)
    data = _company_data("qwen38-flash-next")
    assert on._claude_plan_allowed(on._config_cache["config"], data)
    assert not off._claude_plan_allowed(off._config_cache["config"], data)


def _quota7(v, acct, u7, reset_h):
    now = time.time()
    v.data[f"claude:quota:{acct}"] = json.dumps({
        "observed_at": now, "status": "allowed", "reset": int(now + 3600),
        "five_hour": {"utilization": 0.0, "resets_at": int(now + 3600)},
        "seven_day": {"utilization": u7, "resets_at": int(now + reset_h * 3600)},
        "overage_in_use": False})


def test_nueva_va_a_la_de_mas_margen_por_hora_hasta_su_reinicio():
    """INFRA-1204 (Dani 06-11): correo 41 % con 138 h por delante pierde frente a edani
    32 % que reinicia en 12 h, aunque correo vaya antes en la lista."""
    v = FakeValkey()
    _quota7(v, "info", 1.0, 40)
    _quota7(v, "correo", 0.41, 138)
    _quota7(v, "edani", 0.32, 12)
    mod = _pod(v, policy={"claude-atlantic": [{"id": e, "salto_7d": 100, "salto_5h": 100}
                                                for e in ("info", "correo", "edani")]})
    assert _pick(mod, alias="claude-atlantic", sid="nueva") == ["edani"]


def test_la_lista_sigue_mandando_que_cuentas_y_su_salto():
    v = FakeValkey()
    _quota7(v, "correo", 0.41, 138)
    _quota7(v, "edani", 0.32, 12)
    _quota7(v, "flash", 0.0, 1)  # la de mas margen, pero fuera de la lista de la key
    mod = _pod(v, policy={"claude-x86": [{"id": "correo", "salto_7d": 100, "salto_5h": 100},
                                         {"id": "edani", "salto_7d": 30, "salto_5h": 100}]})
    assert _pick(mod, sid="nueva") == ["correo"]  # edani paso su salto (30)


def test_empezada_no_se_muda_aunque_otra_tenga_mas_margen():
    v = FakeValkey()
    mod = _pod(v)
    assert _pick(mod, sid="s1") == ["info"]
    _quota7(v, "info", 0.5, 100)
    _quota7(v, "correo", 0.0, 2)
    assert _pick(mod, sid="s1") == ["info"]
    assert _pick(mod, sid="otra") == ["correo"]


# ── DGX-1535: ventana semanal de Fable (`7d_oi`) y su salto por cuenta ───────────


def test_lectura_trae_la_ventana_de_fable_si_viene_7d_oi():
    mod = _pod(FakeValkey())
    h = {"anthropic-ratelimit-unified-5h-utilization": "0.1",
         "anthropic-ratelimit-unified-7d-utilization": "0.2",
         "llm_provider-anthropic-ratelimit-unified-7d_oi-utilization": "0.73",
         "llm_provider-anthropic-ratelimit-unified-7d_oi-reset": "1792908000"}
    r = mod.claude_quota_from_headers(h, now=1.0)
    assert r["seven_day_fable"] == {"utilization": 0.73, "resets_at": 1792908000}
    assert r["seven_day"] == {"utilization": 0.2, "resets_at": None}


def test_lectura_sin_7d_oi_deja_fable_a_none_y_lo_demas_igual():
    mod = _pod(FakeValkey())
    h = {"anthropic-ratelimit-unified-5h-utilization": "0.1",
         "anthropic-ratelimit-unified-7d-utilization": "0.2"}
    r = mod.claude_quota_from_headers(h, now=1.0)
    assert r["seven_day_fable"] is None
    assert set(r) == {"observed_at", "status", "reset", "representative_claim", "five_hour",
                      "seven_day", "seven_day_fable", "overage_in_use"}
    # Solo 7d_oi no es una lectura: siguen mandando 5h/7d.
    assert mod.claude_quota_from_headers({"anthropic-ratelimit-unified-7d_oi-utilization": "0.5"}) is None


def _capture(cq, headers, acct="info"):
    kwargs = {"litellm_params": {"model_info": {"id": f"claude-acct-{acct}"}},
              "httpx_response": types.SimpleNamespace(headers=headers)}
    asyncio.run(cq.proxy_handler_instance.async_log_success_event(kwargs, object(), 0, 0))


H_BASE = {"anthropic-ratelimit-unified-5h-utilization": "0.4",
          "anthropic-ratelimit-unified-7d-utilization": "0.2"}


def test_el_callback_conserva_la_ventana_de_fable_vigente():
    valkey = FakeValkey()
    cq = _claude_quota_module(valkey)
    reset = int(time.time()) + 86400
    _capture(cq, {**H_BASE, "anthropic-ratelimit-unified-7d_oi-utilization": "0.8",
                  "anthropic-ratelimit-unified-7d_oi-reset": str(reset)})
    _capture(cq, {**H_BASE, "anthropic-ratelimit-unified-5h-utilization": "0.5"})
    reading = json.loads(valkey.data["claude:quota:info"])
    assert reading["five_hour"]["utilization"] == 0.5          # la lectura nueva manda
    assert reading["seven_day_fable"] == {"utilization": 0.8, "resets_at": reset}
    # Una lectura nueva de Fable si la sustituye.
    _capture(cq, {**H_BASE, "anthropic-ratelimit-unified-7d_oi-utilization": "0.1",
                  "anthropic-ratelimit-unified-7d_oi-reset": str(reset)})
    assert json.loads(valkey.data["claude:quota:info"])["seven_day_fable"]["utilization"] == 0.1


def test_el_callback_no_conserva_la_ventana_de_fable_reiniciada():
    valkey = FakeValkey()
    cq = _claude_quota_module(valkey)
    valkey.data["claude:quota:info"] = json.dumps({
        "seven_day_fable": {"utilization": 0.9, "resets_at": int(time.time()) - 60}})
    _capture(cq, H_BASE)
    assert json.loads(valkey.data["claude:quota:info"])["seven_day_fable"] is None


def test_el_callback_guarda_aunque_falle_el_get_de_fable():
    class GetRoto(FakeValkey):
        async def get(self, key):
            raise ConnectionError("get roto")

    valkey = GetRoto()
    cq = _claude_quota_module(valkey)
    _capture(cq, H_BASE)
    reading = json.loads(valkey.data["claude:quota:info"])
    assert reading["five_hour"]["utilization"] == 0.4 and reading["seven_day_fable"] is None


def _quota_fable(v, acct, uf, reset_in=86400):
    now = time.time()
    v.data[f"claude:quota:{acct}"] = json.dumps({
        "observed_at": now, "status": "allowed", "reset": int(now + 3600),
        "five_hour": {"utilization": 0.1, "resets_at": int(now + 3600)},
        "seven_day": {"utilization": 0.1, "resets_at": int(now + 86400)},
        "seven_day_fable": {"utilization": uf, "resets_at": int(now + reset_in)},
        "overage_in_use": False})


FABLE_POLICY = {"claude-x86": [{"id": "info", "salto_7d": 90, "salto_5h": 90, "salto_fable": 50},
                               {"id": "correo", "salto_7d": 90, "salto_5h": 90, "salto_fable": 50}]}


def _pick_model(mod, model, sid, deps=None, alias="claude-x86"):
    out = asyncio.run(mod.claude_account_filter(model, deps or _deps(), _kw(alias=alias, sid=sid)))
    return [d["model_info"]["id"].removeprefix("claude-acct-") for d in out]


def test_fable_por_encima_de_su_salto_salta_de_cuenta_y_opus_no():
    v = FakeValkey()
    mod = _pod(v, policy=FABLE_POLICY)
    assert _pick_model(mod, "claude-fable-5", "f1") == ["info"]
    assert _pick_model(mod, "claude-opus-5-5", "o1") == ["info"]
    _quota_fable(v, "info", 0.6)                                    # 60 % >= salto_fable 50
    assert _pick_model(mod, "claude-fable-5", "f1") == ["correo"]   # el pin se muda
    assert _pick_model(mod, "claude-Fable-5", "f2") == ["correo"]   # nueva: info no es libre
    assert _pick_model(mod, "claude-opus-5-5", "o1") == ["info"]    # a Opus no le afecta


def test_fable_con_ventana_reiniciada_o_sin_salto_propio_no_salta():
    v = FakeValkey()
    _quota_fable(v, "info", 0.95, reset_in=-60)
    mod = _pod(v, policy=FABLE_POLICY)
    assert _pick_model(mod, "claude-fable-5", "f1") == ["info"]     # reset pasado: no cuenta
    _quota_fable(v, "info", 0.95)
    sin = _pod(v, policy=POLICY)                                    # sin salto_fable => 100
    assert _pick_model(sin, "claude-fable-5", "f9") == ["info"]


def test_salto_fable_ausente_o_basura_es_100():
    mod = _pod(FakeValkey())
    clean = mod._sanitize_claude_accounts({"claude-x86": [
        {"id": "a"}, {"id": "b", "salto_fable": "x"}, {"id": "c", "salto_fable": 140},
        {"id": "d", "salto_fable": True}, {"id": "e", "salto_fable": float("nan")},
        {"id": "f", "salto_fable": 40}]})
    assert [e["salto_fable"] for e in clean["claude-x86"]] == [100.0] * 5 + [40.0]


# ── DGX-1713: un modelo Mythos solo va a las cuentas que lo tienen ───────────────
#
# Sonda 19-11-2026 ~09:20Z (GET /v1/models/claude-mythos-5-{1,2} con el OAT de cada cuenta): 200 en
# work-personal, works-shared, flash, max y sergio; 404 en info, correo y edani. La lista vive en la env
# LITELLM_CLAUDE_MYTHOS_ACCOUNTS (se lee al cargar el modulo, asi que cada test la fija ANTES de `_pod`).

MYTHOS_ENV = "work-personal,works-shared,flash,max,sergio"
TODAS = ("info", "correo", "edani", "flash", "works-shared", "work-personal", "max", "sergio")
MYTHOS = "claude-mythos-5-2"
MYTHOS_POLICY = {"claude-x86": [{"id": a, "salto_7d": 90, "salto_5h": 90, "salto_fable": 50}
                                 for a in ("info", "correo", "sergio")],
                 "claude-atlantic": [{"id": a, "salto_7d": 90, "salto_5h": 90}
                                      for a in ("info", "correo", "edani", "flash", "works-shared", "max", "sergio")]}


def _mythos_pod(monkeypatch, valkey, env=MYTHOS_ENV, policy=MYTHOS_POLICY):
    if env is None:
        monkeypatch.delenv("LITELLM_CLAUDE_MYTHOS_ACCOUNTS", raising=False)
    else:
        monkeypatch.setenv("LITELLM_CLAUDE_MYTHOS_ACCOUNTS", env)
    return _pod(valkey, policy=policy)


def test_mythos_con_politica_info_correo_sergio_solo_va_a_sergio(monkeypatch):
    v = FakeValkey()
    mod = _mythos_pod(monkeypatch, v)
    assert _pick_model(mod, MYTHOS, "m1", deps=_deps(TODAS)) == ["sergio"]
    assert v.data["claude_account_pin:v1:claude-x86:m1"] == "sergio"
    # Opus, con la misma politica y la env puesta, sigue la regla de siempre: la primera de la lista.
    assert _pick_model(mod, "claude-opus-5-5", "o1", deps=_deps(TODAS)) == ["info"]


def test_mythos_nunca_sale_de_la_lista_de_la_key(monkeypatch):
    # flash tiene Mythos pero no esta en la politica de claude-x86: no se usa.
    mod = _mythos_pod(monkeypatch, FakeValkey(), policy={"claude-x86": MYTHOS_POLICY["claude-x86"][:2]})
    assert _pick_model(mod, MYTHOS, "m1", deps=_deps(TODAS)) == []


def test_mythos_con_la_cuenta_buena_sin_deployment_sano_no_cae_a_una_sin_mythos(monkeypatch):
    mod = _mythos_pod(monkeypatch, FakeValkey())
    assert _pick_model(mod, MYTHOS, "m1", deps=_deps(("info", "correo", "edani"))) == []
    assert _pick_model(mod, MYTHOS, "m2", deps=_deps(("info", "correo"))) == []


def test_mythos_entre_las_elegibles_manda_el_orden_el_salto_y_el_margen(monkeypatch):
    v = FakeValkey()
    mod = _mythos_pod(monkeypatch, v)
    deps = _deps(TODAS)
    # sin lecturas: empate -> orden de la politica de claude-atlantic (flash antes que works-shared, max, sergio)
    assert _pick_model(mod, MYTHOS, "m1", deps=deps, alias="claude-atlantic") == ["flash"]
    _quota(v, "flash", u7=0.95)                      # pasada de su salto de 90
    assert _pick_model(mod, MYTHOS, "m2", deps=deps, alias="claude-atlantic") == ["works-shared"]
    _quota(v, "max", u7=0.0)                         # mas margen por hora que works-shared (10 %)
    _quota(v, "works-shared", u7=0.5)
    assert _pick_model(mod, MYTHOS, "m3", deps=deps, alias="claude-atlantic") == ["max"]
    _quota(v, "max", u5=1.0, status="rejected")      # rechazada: parada como siempre
    _quota(v, "sergio", u7=0.95)
    assert _pick_model(mod, MYTHOS, "m4", deps=deps, alias="claude-atlantic") == ["works-shared"]
    _quota(v, "works-shared", u7=0.95)
    assert _pick_model(mod, MYTHOS, "m5", deps=deps, alias="claude-atlantic") == []   # todas pasadas de salto


def test_mythos_con_pin_en_cuenta_sin_mythos_se_reelige_y_el_pin_muda(monkeypatch):
    v = FakeValkey()
    mod = _mythos_pod(monkeypatch, v)
    deps = _deps(TODAS)
    assert _pick_model(mod, "claude-opus-5-5", "p1", deps=deps) == ["info"]
    assert v.data["claude_account_pin:v1:claude-x86:p1"] == "info"
    assert _pick_model(mod, MYTHOS, "p1", deps=deps) == ["sergio"]            # info no vale para Mythos
    assert v.data["claude_account_pin:v1:claude-x86:p1"] == "sergio"
    assert mod.CLAUDE_ACCOUNT_STATS["moves"] == 1
    assert _pick_model(mod, MYTHOS, "p1", deps=deps) == ["sergio"]            # ahora acierta el pin
    assert mod.CLAUDE_ACCOUNT_STATS["hits"] == 1
    assert _pick_model(mod, "claude-opus-5-5", "p1", deps=deps) == ["sergio"]  # y la conversacion sigue ahi


def test_mythos_con_pin_en_cuenta_con_mythos_se_queda_hasta_su_salto(monkeypatch):
    v = FakeValkey()
    mod = _mythos_pod(monkeypatch, v, policy={"claude-x86": [
        {"id": a, "salto_7d": 90, "salto_5h": 90} for a in ("info", "sergio", "flash")]})
    deps = _deps(TODAS)
    assert _pick_model(mod, MYTHOS, "q1", deps=deps) == ["sergio"]
    _quota(v, "flash", u7=0.0)                                                # flash tendria mas margen
    assert _pick_model(mod, MYTHOS, "q1", deps=deps) == ["sergio"]            # el pin manda mientras valga
    _quota(v, "sergio", u7=0.95)                                              # pasa su salto: muda a otra CON Mythos
    assert _pick_model(mod, MYTHOS, "q1", deps=deps) == ["flash"]


def test_mythos_sin_la_env_no_tiene_cuenta_y_opus_no_se_entera(monkeypatch, caplog):
    v = FakeValkey()
    mod = _mythos_pod(monkeypatch, v, env=None)
    deps = _deps(TODAS)
    with caplog.at_level(logging.WARNING, logger="session_router"):
        assert _pick_model(mod, MYTHOS, "e1", deps=deps) == []
    assert "LITELLM_CLAUDE_MYTHOS_ACCOUNTS" in caplog.text and "falla cerrado" in caplog.text
    assert "claude_account_pin:v1:claude-x86:e1" not in v.data
    assert _pick_model(mod, "claude-opus-5-5", "e2", deps=deps) == ["info"]
    assert _pick_model(mod, "claude-fable-5", "e3", deps=deps) == ["info"]


def test_env_vacia_o_basura_es_lo_mismo_que_sin_env(monkeypatch, caplog):
    for env in ("", "  ,, ", "Sergio,work personal,!!", "sergio;flash"):
        mod = _mythos_pod(monkeypatch, FakeValkey(), env=env)
        assert mod.CLAUDE_MYTHOS_ACCOUNTS == frozenset(), env
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="session_router"):
            assert _pick_model(mod, MYTHOS, "g1", deps=_deps(TODAS)) == [], env
        assert "falla cerrado" in caplog.text
        assert _pick_model(mod, "claude-opus-5-5", "g2", deps=_deps(TODAS)) == ["info"], env


def test_env_con_algun_id_valido_descarta_solo_la_basura_y_no_vuelca_el_valor(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="session_router"):
        mod = _mythos_pod(monkeypatch, FakeValkey(), env=" sergio , ,Flash,x y,flash")
    assert mod.CLAUDE_MYTHOS_ACCOUNTS == frozenset({"sergio", "flash"})
    assert "descarto 2 entrada" in caplog.text and "Flash" not in caplog.text
    assert _pick_model(mod, MYTHOS, "g1", deps=_deps(TODAS)) == ["sergio"]


def test_la_env_se_lee_una_vez_al_cargar_el_modulo(monkeypatch):
    mod = _mythos_pod(monkeypatch, FakeValkey())
    monkeypatch.setenv("LITELLM_CLAUDE_MYTHOS_ACCOUNTS", "flash")
    assert _pick_model(mod, MYTHOS, "l1", deps=_deps(TODAS)) == ["sergio"]    # la de la carga, no la de ahora


def test_mythos_no_distingue_mayusculas_ni_version(monkeypatch):
    mod = _mythos_pod(monkeypatch, FakeValkey())
    deps = _deps(TODAS)
    for i, m in enumerate(("claude-Mythos-5-2", "claude-MYTHOS-5-1", "claude-mythos-5-2[1m]", "claude-mythos-6")):
        assert _pick_model(mod, m, f"c{i}", deps=deps) == ["sergio"], m


def test_opus_y_fable_se_comportan_igual_con_la_env_puesta(monkeypatch):
    v = FakeValkey()
    mod = _mythos_pod(monkeypatch, v, policy=FABLE_POLICY)
    # Mismo guion que test_fable_por_encima_de_su_salto_salta_de_cuenta_y_opus_no: info no tiene Mythos
    # y Fable/Opus la siguen usando.
    assert _pick_model(mod, "claude-fable-5", "f1") == ["info"]
    assert _pick_model(mod, "claude-opus-5-5", "o1") == ["info"]
    _quota_fable(v, "info", 0.6)
    assert _pick_model(mod, "claude-fable-5", "f1") == ["correo"]
    assert _pick_model(mod, "claude-Fable-5", "f2") == ["correo"]
    assert _pick_model(mod, "claude-opus-5-5", "o1") == ["info"]


def test_la_ventana_de_fable_no_aplica_a_mythos(monkeypatch):
    """Mythos no trae las cabeceras 7d_oi: una cuenta con la ventana de Fable por encima de salto_fable
    sigue sirviendo Mythos, y Fable en la misma cuenta si salta."""
    v = FakeValkey()
    mod = _mythos_pod(monkeypatch, v)
    deps = _deps(TODAS)
    _quota_fable(v, "sergio", 0.9)                                           # 90 % >= salto_fable 50
    assert _pick_model(mod, MYTHOS, "w1", deps=deps) == ["sergio"]
    assert _pick_model(mod, "claude-fable-5", "w2", deps=deps) == ["info"]   # info, la primera; sergio ni cuenta
    assert _pick_model(mod, "claude-fable-5", "w3", deps=_deps(("sergio",))) == []
