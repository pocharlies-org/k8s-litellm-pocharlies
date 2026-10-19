"""La cuenta ChatGPT Plus de Dani por litellm (DGX-1053): siete alias de chat, tres de imagen
(DGX-1077), un gate y un renovador.

POR QUE EXISTE ESTE FICHERO (18-10-2026). Los alias `gpt-*` vuelven al model_list tras
su lapida del 03-09: aquella cuenta cayo baneada dos veces porque el catalogo de
airsoft le mandaba imagenes por el desvio de vision. Ahora van contra la cuenta
personal de Dani y lo que impide repetirlo es esto: ningun camino automatico apunta a
ellos y un gate fail-closed por prefijo solo deja pasar a `claude-local`, `hermes` y
`studio-chatgpt` (los motores de imagen del DGX Studio)
(la key `sauvage-shield` lleva `gpt-5.6-luna` en su allowlist desde agosto).

Y el token tiene un unico renovador, el CronJob chatgpt-token-keeper: OpenAI rota el
refresh token y el provider no tiene cerrojo, asi que dos replicas renovando a la vez
revocan la sesion. Los pods lo montan en solo lectura.
"""
import base64
import json
import time
import types
import urllib.error
from pathlib import Path

import pytest
import yaml

from manifest_docs import hook_gate


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
KEEPER = ROOT / "k8s" / "chatgpt-token-keeper.yaml"

ALIASES = {"gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
           "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
IMAGE_ALIASES = {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst", "gpt-image-2"}
ALLOWED_KEYS = ("claude-local", "hermes", "studio-chatgpt", "claude-casa", "hindsight")  # DGX-1099: claude-casa; INFRA-942: hindsight
WANT_FN = {"_chatgpt_access_denied"}
WANT_CONST = {"CHATGPT_ALIAS_PREFIX", "CHATGPT_ALLOWED_KEY_ALIASES"}


def _docs(path=MANIFEST):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _config():
    cm = next(d for d in _docs() if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    return cm, yaml.safe_load(cm["data"]["config.yaml"])


def _litellm_pod():
    dep = next(d for d in _docs() if d.get("kind") == "Deployment" and d["metadata"]["name"] == "litellm")
    spec = dep["spec"]["template"]["spec"]
    return spec, next(c for c in spec["containers"] if c["name"] == "litellm")


def _gate(env):
    return hook_gate(WANT_FN, WANT_CONST, env, "chatgptgate")


# ── alias ────────────────────────────────────────────────────────────────────

def test_the_seven_aliases_go_to_the_chatgpt_provider_for_free():
    _, cfg = _config()
    by_name = {m["model_name"]: m for m in cfg["model_list"]}
    assert ALIASES <= set(by_name)
    for name in ALIASES:
        entry = by_name[name]
        assert entry["litellm_params"]["model"] == f"chatgpt/{name}"
        assert "api_key" not in entry["litellm_params"], name
        assert entry["model_info"]["input_cost_per_token"] == 0
        assert entry["model_info"]["output_cost_per_token"] == 0
        # background_health_checks esta activo: sin esto cada ronda gastaria cuota de la cuenta.
        assert entry["model_info"]["disable_background_health_check"] is True



def test_the_image_aliases_go_to_the_chatgpt_provider_for_free_and_unprobed():
    _, cfg = _config()
    by_name = {m["model_name"]: m for m in cfg["model_list"]}
    for name in IMAGE_ALIASES:
        entry = by_name[name]
        assert entry["litellm_params"] == {"model": f"chatgpt/{name}"}, name
        assert entry["model_info"]["mode"] == "image_generation"
        assert entry["model_info"]["input_cost_per_image"] == 0
        # un health check de imagen generaria una imagen por ronda contra la cuota de la cuenta
        assert entry["model_info"]["disable_background_health_check"] is True

def test_no_automatic_path_reaches_a_gpt_alias():
    _, cfg = _config()
    rs = cfg.get("router_settings", {}) or {}
    names = set()
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks", "default_fallbacks"):
        for edge in rs.get(key) or []:
            if isinstance(edge, dict):
                for source, targets in edge.items():
                    names.update([source, *(targets or [])])
            else:
                names.add(edge)
    names.update((rs.get("model_group_alias") or {}).values())
    names.update((rs.get("model_group_alias") or {}).keys())
    assert not {n for n in names if str(n).startswith("gpt-")}, "un camino automatico llega a gpt-*"
    raw = _config()[0]["data"]["config.yaml"]
    for line in raw.splitlines():
        if "model_name:" in line or line.strip().startswith("#"):
            continue
        for alias in ALIASES | IMAGE_ALIASES:
            if f"model: chatgpt/{alias}" in line:
                continue
            assert f" {alias}" not in line and f"'{alias}'" not in line and f'"{alias}"' not in line, line


def test_the_vision_divert_never_reaches_a_gpt_alias():
    """El camino de los baneos de agosto fue el desvio de VISION del hook, no el
    config.yaml: CAPABILITY_CHAINS decide que se desvia (VISION_DIVERTIBLE), a donde
    (VISION_FALLBACK_MODEL) y con que red (TOOLING_FALLBACKS). Ningun gpt-* ahi."""
    import ast as _ast

    tree = _ast.parse(_config()[0]["data"]["litellm_strip_params.py"])
    wanted = {"CAPABILITY_CHAINS", "VISION_DIVERTIBLE", "VISION_FALLBACK_MODEL", "TOOLING_FALLBACKS"}
    found = {}
    for node in tree.body:
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) in wanted:
                    found[target.id] = {
                        c.value for c in _ast.walk(node.value)
                        if isinstance(c, _ast.Constant) and isinstance(c.value, str)
                    }
    assert wanted <= set(found), f"el hook ya no define: {sorted(wanted - set(found))}"
    for name, strings in found.items():
        assert not {v for v in strings if v.startswith("gpt-")}, f"{name} llega a un gpt-*"
    # VISION_DIVERTIBLE se deriva de CAPABILITY_CHAINS; si pasara a literal, lo cubre el bucle de arriba.
    _, c = _litellm_pod()
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert not str(env.get("LITELLM_VISION_FALLBACK_MODEL", "")).startswith("gpt-")


# ── gate ─────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def open_gate():
    return _gate({"LITELLM_CHATGPT_ALLOWED_KEYS": ",".join(ALLOWED_KEYS)})


def test_a_deployment_without_the_variable_denies_everyone():
    gate = _gate({})
    for key in ALLOWED_KEYS + ("master", "sauvage-shield"):
        denied, detail = gate._chatgpt_access_denied("gpt-6.1-sol", key)
        assert denied and detail["allowed"] == []


def test_only_the_allowed_keys_get_through(open_gate):
    for alias in ALIASES | IMAGE_ALIASES:
        for key in ALLOWED_KEYS:
            assert open_gate._chatgpt_access_denied(alias, key) == (False, None)
        for key in ("sauvage-shield", "master", "synapse", "document-intake", "hermes-batch", None):
            denied, _ = open_gate._chatgpt_access_denied(alias, key)
            assert denied, f"{key!r} paso a {alias}"


def test_a_future_gpt_alias_is_already_closed(open_gate):
    assert open_gate._chatgpt_access_denied("gpt-7-sol", "sauvage-shield")[0]


def test_the_gate_ignores_everything_else(open_gate):
    for alias in ("tooling", "qwen38-flash-next", "or-glm", "grok-imagine", "", None):
        assert open_gate._chatgpt_access_denied(alias, "sauvage-shield") == (False, None)


def test_the_hook_calls_the_gate_and_the_deployment_ships_the_allowlist():
    cm, _ = _config()
    assert "_denied, _detail = _chatgpt_access_denied(" in cm["data"]["litellm_strip_params.py"]
    _, c = _litellm_pod()
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert set(env["LITELLM_CHATGPT_ALLOWED_KEYS"].split(",")) == set(ALLOWED_KEYS)
    assert env["CHATGPT_TOKEN_DIR"] == "/var/run/chatgpt-oauth"


# ── token ────────────────────────────────────────────────────────────────────

def test_pods_mount_the_keeper_secret_read_only_and_never_a_network_volume():
    spec, c = _litellm_pod()
    mount = next(m for m in c["volumeMounts"] if m["name"] == "chatgpt-oauth")
    assert mount["mountPath"] == "/var/run/chatgpt-oauth" and mount["readOnly"] is True
    vol = next(v for v in spec["volumes"] if v["name"] == "chatgpt-oauth")
    assert vol == {"name": "chatgpt-oauth",
                   "secret": {"secretName": "litellm-chatgpt-token", "optional": True, "defaultMode": 0o400}}
    # SC-381: nada de PVC/NFS en el pod del router.
    assert not any("persistentVolumeClaim" in v or "nfs" in v for v in spec["volumes"])


def test_plan_gateway_does_not_get_the_token():
    dep = next(d for d in _docs() if d.get("kind") == "Deployment" and d["metadata"]["name"] == "plan-gateway")
    assert "chatgpt" not in json.dumps(dep["spec"]["template"]["spec"])


def test_keeper_shape():
    docs = {d["kind"]: d for d in _docs(KEEPER)}
    es = docs["ExternalSecret"]["spec"]
    assert es["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "onepassword-connect"}
    assert es["refreshPolicy"] == "OnChange"
    assert es["data"] == [{"secretKey": "auth.json",
                           "remoteRef": {"key": "litellm", "property": "CHATGPT_OAUTH_AUTH_JSON"}}]
    cj = docs["CronJob"]["spec"]
    assert cj["concurrencyPolicy"] == "Forbid"
    assert cj["jobTemplate"]["spec"]["backoffLimit"] == 0
    rules = docs["Role"]["rules"]
    assert {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["litellm-chatgpt-token"],
            "verbs": ["get", "patch"]} in rules


# ── keeper, ejecutado ───────────────────────────────────────────────────────

def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


def _keeper(tmp_path, secret, refresh_response=None, refresh_error=None):
    src = next(d for d in _docs(KEEPER) if d["kind"] == "ConfigMap")["data"]["chatgpt_token_keeper.py"]
    mod = types.ModuleType("keeper")
    exec(compile(src, "keeper", "exec"), mod.__dict__)  # noqa: S102
    calls = []
    store = {"secret": secret}

    def kube(method, path="", body=None):
        calls.append((method, body))
        if method == "GET":
            return store["secret"]
        if method in ("POST", "PATCH"):
            store["secret"] = {"data": body["data"]}
            return {}
        raise AssertionError(method)

    def refresh(record):
        calls.append(("REFRESH", record["refresh_token"]))
        if refresh_error:
            raise refresh_error
        return refresh_response

    mod.kube = kube
    mod.refresh = refresh
    mod.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)
    return mod, calls, store


def _record(hours_left):
    exp = int(time.time() + hours_left * 3600)
    return {"access_token": _jwt(exp), "refresh_token": "rt-old", "id_token": "id",
            "expires_at": exp, "account_id": "acc"}


def _as_secret(record):
    return {"data": {"auth.json": base64.b64encode(json.dumps(record).encode()).decode()}}


def test_first_run_creates_the_secret_from_the_seed_and_does_not_renew(tmp_path):
    seed = tmp_path / "auth.json"
    seed.write_text(json.dumps(_record(200)))
    mod, calls, store = _keeper(tmp_path, None)
    mod.SEED = str(seed)
    assert mod.main() == 0
    assert [c[0] for c in calls] == ["GET", "POST"]
    assert json.loads(base64.b64decode(store["secret"]["data"]["auth.json"]))["refresh_token"] == "rt-old"


def test_far_from_expiry_nothing_happens(tmp_path):
    mod, calls, _ = _keeper(tmp_path, _as_secret(_record(100)))
    assert mod.main() == 0
    assert [c[0] for c in calls] == ["GET"]


def test_near_expiry_renews_once_and_stores_the_rotated_token(tmp_path):
    new = _record(240) | {"refresh_token": "rt-new"}
    mod, calls, store = _keeper(tmp_path, _as_secret(_record(10)), refresh_response=new)
    assert mod.main() == 0
    assert [c[0] for c in calls] == ["GET", "REFRESH", "PATCH"]
    assert json.loads(base64.b64decode(store["secret"]["data"]["auth.json"]))["refresh_token"] == "rt-new"


def test_a_rejected_renewal_fails_loudly_and_writes_nothing(tmp_path):
    err = urllib.error.HTTPError("u", 401, "no", {}, None)
    err.read = lambda: b'{"error":"refresh_token_reused"}'
    mod, calls, _ = _keeper(tmp_path, _as_secret(_record(10)), refresh_error=err)
    assert mod.main() == 1
    assert "PATCH" not in [c[0] for c in calls]


def test_the_keeper_never_prints_tokens(tmp_path, capsys):
    new = _record(240) | {"refresh_token": "rt-new"}
    mod, _, _ = _keeper(tmp_path, _as_secret(_record(10)), refresh_response=new)
    mod.main()
    out = capsys.readouterr()
    assert "rt-" not in out.out + out.err and "h." not in out.out + out.err


def test_refresh_sends_the_provider_body_and_keeps_the_old_refresh_token_if_none_comes(tmp_path):
    src = next(d for d in _docs(KEEPER) if d["kind"] == "ConfigMap")["data"]["chatgpt_token_keeper.py"]
    mod = types.ModuleType("keeper")
    exec(compile(src, "keeper", "exec"), mod.__dict__)  # noqa: S102
    sent = {}
    new_exp = int(time.time() + 240 * 3600)

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"access_token": _jwt(new_exp), "id_token": "id2"}).encode()

    def urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data)
        return Resp()

    mod.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=__import__("urllib.request").request.Request,
                                                                     urlopen=urlopen),
                                       error=urllib.error)
    out = mod.refresh(_record(10))
    assert sent["url"] == "https://auth.openai.com/oauth/token"
    assert sent["body"] == {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann", "grant_type": "refresh_token",
                            "refresh_token": "rt-old", "scope": "openid profile email"}
    assert out["refresh_token"] == "rt-old" and out["expires_at"] == new_exp and out["account_id"] == "acc"
