"""GPT-Live sale con voz ember salvo que el cliente pida otra voz (DGX-1808).

Lo decidió Dani el 23-11-2026 tras oír las nueve voces de ChatGPT. El default vive en el hook (architect,
23-11). El acento no: el backend de ChatGPT ignora `session.instructions`, y la persona se guía desde el
cliente con `session.context.append` (canal `commentary`).
"""
import ast
import pathlib
import re
import types

import pytest
import yaml

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
WANT = {"LIVE_SESSION_DEFAULT_VOICE", "_apply_live_session_defaults"}
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
    assert len(keep) == len(WANT), "el hook ya no define LIVE_SESSION_DEFAULT_VOICE y su helper"
    mod = types.ModuleType("hook_live_defaults")
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<hook>", "exec"), mod.__dict__)  # noqa: S102
    return mod


def _data(**session):
    return {"model": "gpt-live-1-codex", "session": {"model": "gpt-live-1-codex", **session}}


def test_sin_voz_sale_ember(hook):
    d = _data()
    hook._apply_live_session_defaults(d)
    assert d["session"]["audio"]["output"]["voice"] == "ember"


def test_la_voz_del_cliente_gana(hook):
    d = _data(audio={"output": {"voice": "juniper"}})
    hook._apply_live_session_defaults(d)
    assert d["session"]["audio"]["output"]["voice"] == "juniper"


def test_no_toca_las_instrucciones_del_cliente(hook):
    d = _data(instructions="Eres Nova.")
    hook._apply_live_session_defaults(d)
    assert d["session"]["instructions"] == "Eres Nova."


def test_otros_modelos_no_se_tocan(hook):
    d = {"model": "gpt-6-luna", "session": {"model": "gpt-6-luna"}}
    hook._apply_live_session_defaults(d)
    assert d["session"] == {"model": "gpt-6-luna"}


def test_las_voces_por_defecto_son_de_chatgpt(hook):
    for alias, voice in hook.LIVE_SESSION_DEFAULT_VOICE.items():
        assert voice in CHATGPT_VOICES, f"{alias}: {voice} no es una voz de ChatGPT"


def test_el_hook_lo_aplica_en_las_llamadas_realtime(hook_src):
    assert re.search(r'if call_type == "arealtime_calls":\s*\n\s*_apply_live_session_defaults\(data\)', hook_src)
