import ast
import logging
import urllib.parse
from pathlib import Path
from textwrap import dedent


MANIFEST = Path(__file__).parents[1] / "k8s" / "manifest.yaml"
REQUIRED_MODELS = ("ornith-canary", "ornith-1.0", "dense-uncensored", "router")


def load_reconciler(request_json, log):
    manifest = MANIFEST.read_text()
    source = dedent(manifest.split("  sync.py: |\n", 1)[1].split("\n---\n", 1)[0])
    module = ast.parse(source)
    reconciler = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "reconcile_required_team_models"
    )
    namespace = {
        "LITELLM_BASE_URL": "http://litellm.test",
        "OPENCLAW_TEAM_ID": "openclaw",
        "OPENCLAW_KEY_ALIAS": "openclaw-qwen36-prod",
        "OPENCLAW_TEAM_REQUIRED_MODELS": REQUIRED_MODELS,
        "_request_json": request_json,
        "_litellm_headers": lambda: {"Authorization": "Bearer test"},
        "urllib": urllib,
        "log": log,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[reconciler], type_ignores=[])), "sync.py", "exec"), namespace)
    return namespace["reconcile_required_team_models"]


def test_reconciler_updates_only_the_named_openclaw_virtual_key():
    calls = []
    team_data = {
        "team_info": {"models": list(REQUIRED_MODELS)},
        "keys": [
            {
                "key_alias": "openclaw-qwen36-prod",
                "token": "hashed-openclaw-key",
                "models": ["qwen36-35b-tooling", "dense"],
            },
            {
                "key_alias": "other-openclaw-key",
                "token": "hashed-other-key",
                "models": ["qwen36-35b-tooling"],
            },
        ],
    }

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if method == "GET":
            return team_data
        return {}

    load_reconciler(request_json, logging.getLogger(__name__))()

    assert calls[0][0:2] == ("GET", "http://litellm.test/team/info?team_id=openclaw")
    assert calls[1] == (
        "POST",
        "http://litellm.test/key/update",
        {
            "key": "hashed-openclaw-key",
            "models": [
                "qwen36-35b-tooling",
                "dense",
                "ornith-canary",
                "ornith-1.0",
                "dense-uncensored",
                "router",
            ],
        },
        {"Authorization": "Bearer test"},
    )
    assert len(calls) == 2


def test_reconciler_is_idempotent_when_the_named_key_already_has_the_aliases():
    calls = []
    team_data = {
        "team_info": {"models": list(REQUIRED_MODELS)},
        "keys": [
            {
                "key_alias": "openclaw-qwen36-prod",
                "token": "hashed-openclaw-key",
                "models": ["qwen36-35b-tooling", *REQUIRED_MODELS],
            }
        ],
    }

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        return team_data if method == "GET" else {}

    load_reconciler(request_json, logging.getLogger(__name__))()

    assert calls == [
        (
            "GET",
            "http://litellm.test/team/info?team_id=openclaw",
            None,
            {"Authorization": "Bearer test"},
        )
    ]
