"""GPT-Live sale con voz ember y acento de España salvo que el cliente pida otra voz (DGX-1808).

Lo decidió Dani el 23-11-2026 tras oír las nueve voces de ChatGPT. El default vive en el hook (architect,
23-11), y solo vale al crear la sesión: después el servidor rechaza cambiar `instructions`.
"""
import ast
import pathlib
import re
import types

import pytest
import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
WANT = {"LIVE_SESSION_DEFAULTS", "_apply_live_session_defaults"}
# Las de ChatGPT: una voz de la API (marin, cedar) deja la llamada colgada sin error (medido el 23-11).
CHATGPT_VOICES = {"arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale"}


@pytest.fixture(scope="module")
def hook_src():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config":
            return doc["data"]["litellm_strip_params.py"]
    raise AssertionError("no encuentro el ConfigMap litellm-config")


@pytest.fixture(scope="module")
def hook(hook_src):
    keep = [n for n in ast.parse(hook_src).body
            if (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in WANT for t in n.targets))
            or (isinstance(n, ast.FunctionDef) and n.name in WANT)]
    assert len(keep) == len(WANT), "el hook ya no define LIVE_SESSION_DEFAULTS y su helper"
    mod = types.ModuleType("hook_live_defaults")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


def _data(**session):
    return {"model": "gpt-live-1-codex", "session": {"model": "gpt-live-1-codex", **session}}


def test_sin_voz_sale_ember_con_acento(hook):
    d = _data()
    hook._apply_live_session_defaults(d)
    assert d["session"]["audio"]["output"]["voice"] == "ember"
    assert "acento peninsular" in d["session"]["instructions"]


def test_la_voz_del_cliente_gana(hook):
    d = _data(audio={"output": {"voice": "juniper"}})
    hook._apply_live_session_defaults(d)
    assert d["session"]["audio"]["output"]["voice"] == "juniper"


def test_las_instrucciones_del_cliente_se_conservan_detras_del_acento(hook):
    d = _data(instructions="Eres Nova, una asistente en una reunión.")
    hook._apply_live_session_defaults(d)
    ins = d["session"]["instructions"]
    assert ins.startswith(hook.LIVE_SESSION_DEFAULTS["gpt-live-1-codex"]["instructions"])
    assert ins.endswith("Eres Nova, una asistente en una reunión.")


def test_otros_modelos_no_se_tocan(hook):
    d = {"model": "gpt-6-luna", "session": {"model": "gpt-6-luna"}}
    hook._apply_live_session_defaults(d)
    assert d["session"] == {"model": "gpt-6-luna"}


def test_las_voces_por_defecto_son_de_chatgpt(hook):
    for alias, cfg in hook.LIVE_SESSION_DEFAULTS.items():
        assert cfg["voice"] in CHATGPT_VOICES, f"{alias}: {cfg['voice']} no es una voz de ChatGPT"


def test_el_hook_lo_aplica_en_las_llamadas_realtime(hook_src):
    assert re.search(r'if call_type == "arealtime_calls":\s*\n\s*_apply_live_session_defaults\(data\)', hook_src)
