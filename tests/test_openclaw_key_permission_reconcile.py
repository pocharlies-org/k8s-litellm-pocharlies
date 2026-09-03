import ast
import logging
import urllib.parse
from pathlib import Path
from textwrap import dedent
from typing import Any


MANIFEST = Path(__file__).parents[1] / "k8s" / "manifest.yaml"
REQUIRED_MODELS = (
    "ornith-canary",
    "ornith-1.0",
    "dense-uncensored",
    "tooling",
    "qwen35-4b",
)
SAUVAGE_REQUIRED_MODELS = ("tooling", "ornith-1.0", "qwen35-4b")


def load_reconciler(request_json, log):
    manifest = MANIFEST.read_text()
    source = dedent(manifest.split("  sync.py: |\n", 1)[1].split("\n---\n", 1)[0])
    module = ast.parse(source)
    funcs = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("reconcile_required_team_models", "_reconcile_key_models")
    }
    namespace = {
        "LITELLM_BASE_URL": "http://litellm.test",
        "OPENCLAW_TEAM_ID": "openclaw",
        "OPENCLAW_KEY_ALIAS": "openclaw-qwen36-prod",
        "OPENCLAW_TEAM_REQUIRED_MODELS": REQUIRED_MODELS,
        "SAUVAGE_KEY_ALIAS": "sauvage-shield",
        "SAUVAGE_KEY_REQUIRED_MODELS": SAUVAGE_REQUIRED_MODELS,
        "Any": Any,
        "_request_json": request_json,
        "_litellm_headers": lambda: {"Authorization": "Bearer test"},
        "urllib": urllib,
        "log": log,
    }
    body = [funcs["_reconcile_key_models"], funcs["reconcile_required_team_models"]]
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
            "sync.py",
            "exec",
        ),
        namespace,
    )
    return namespace["reconcile_required_team_models"]


def _team_data(keys, required=None):
    return {
        "team_info": {"models": list(required or REQUIRED_MODELS)},
        "keys": keys,
    }


OPENCLAW_KEY_READY = {
    "key_alias": "openclaw-qwen36-prod",
    "token": "hashed-openclaw-key",
    "models": ["qwen36-35b-tooling", *REQUIRED_MODELS],
}


def test_reconciler_updates_only_the_named_openclaw_virtual_key():
    calls = []
    team_data = _team_data(
        [
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
        ]
    )

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if method == "GET":
            if "/team/info" in url:
                return team_data
            if "/key/list" in url:
                return {"keys": []}
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
                "tooling",
                "qwen35-4b",
            ],
        },
        {"Authorization": "Bearer test"},
    )
    # sauvage-shield no esta en el payload del team: se consulta por /key/list y,
    # al no existir en el cluster de prueba, no recibe POST.
    assert calls[2][0:2] == (
        "GET",
        "http://litellm.test/key/list?key_alias=sauvage-shield&return_full_object=true",
    )
    assert len(calls) == 3


def test_reconciler_is_idempotent_when_the_named_key_already_has_the_aliases():
    calls = []
    team_data = _team_data([OPENCLAW_KEY_READY])

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if method == "GET":
            if "/team/info" in url:
                return team_data
            if "/key/list" in url:
                return {"keys": []}
        return {}

    load_reconciler(request_json, logging.getLogger(__name__))()

    assert calls == [
        (
            "GET",
            "http://litellm.test/team/info?team_id=openclaw",
            None,
            {"Authorization": "Bearer test"},
        ),
        (
            "GET",
            "http://litellm.test/key/list?key_alias=sauvage-shield&return_full_object=true",
            None,
            {"Authorization": "Bearer test"},
        ),
    ]


def test_sauvage_key_is_widened_via_key_list_without_touching_openclaw():
    calls = []
    team_data = _team_data([OPENCLAW_KEY_READY])
    sauvage_data = {
        "keys": [
            {
                "key_alias": "sauvage-shield",
                "token": "hashed-sauvage-key",
                "models": ["tooling"],
            }
        ],
        "total_count": 1,
    }

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if method == "GET":
            if "/team/info" in url:
                return team_data
            if "/key/list" in url:
                return sauvage_data
        return {}

    load_reconciler(request_json, logging.getLogger(__name__))()

    # OpenClaw ya tenia todo -> sin POST para el; el unico POST es de sauvage.
    update_calls = [c for c in calls if c[0] == "POST"]
    assert len(update_calls) == 1
    assert update_calls[0] == (
        "POST",
        "http://litellm.test/key/update",
        {
            "key": "hashed-sauvage-key",
            "models": ["tooling", "ornith-1.0", "qwen35-4b"],
        },
        {"Authorization": "Bearer test"},
    )
    assert calls[0][0:2] == ("GET", "http://litellm.test/team/info?team_id=openclaw")


def test_sauvage_reconcile_is_idempotent_when_already_correct():
    calls = []
    team_data = _team_data([OPENCLAW_KEY_READY])
    sauvage_data = {
        "keys": [
            {
                "key_alias": "sauvage-shield",
                "token": "hashed-sauvage-key",
                "models": list(SAUVAGE_REQUIRED_MODELS),
            }
        ],
        "total_count": 1,
    }

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if method == "GET":
            if "/team/info" in url:
                return team_data
            if "/key/list" in url:
                return sauvage_data
        return {}

    load_reconciler(request_json, logging.getLogger(__name__))()

    # Nada que ensanchar: ni un solo POST.
    assert all(c[0] == "GET" for c in calls), calls
    assert calls[0][0:2] == ("GET", "http://litellm.test/team/info?team_id=openclaw")
    assert calls[1][0:2] == (
        "GET",
        "http://litellm.test/key/list?key_alias=sauvage-shield&return_full_object=true",
    )


def test_sauvage_reconcile_never_removes_models():
    calls = []
    team_data = _team_data([OPENCLAW_KEY_READY])
    # La key ya tiene MAS de lo declarado (un modelo retirado): el
    # reconciliador solo ensancha, nunca quita.
    sauvage_data = {
        "keys": [
            {
                "key_alias": "sauvage-shield",
                "token": "hashed-sauvage-key",
                "models": ["tooling", "extra-model", "qwen35-4b"],
            }
        ],
        "total_count": 1,
    }

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if method == "GET":
            if "/team/info" in url:
                return team_data
            if "/key/list" in url:
                return sauvage_data
        return {}

    load_reconciler(request_json, logging.getLogger(__name__))()

    update_calls = [c for c in calls if c[0] == "POST"]
    assert len(update_calls) == 1
    models = update_calls[0][2]["models"]
    assert "extra-model" in models, "el reconciliador quito un modelo"
    assert models == ["tooling", "extra-model", "qwen35-4b", "ornith-1.0"]


def test_sauvage_key_is_reconciled_when_it_is_a_team_member():
    calls = []
    team_data = _team_data(
        [
            OPENCLAW_KEY_READY,
            {
                "key_alias": "sauvage-shield",
                "token": "hashed-sauvage-key",
                "models": ["tooling"],
            },
        ]
    )

    def request_json(method, url, payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if method == "GET":
            if "/team/info" in url:
                return team_data
            if "/key/list" in url:
                return {"keys": []}
        return {}

    load_reconciler(request_json, logging.getLogger(__name__))()

    # Encontrada en el payload del team: no se consulta /key/list.
    assert not any("/key/list" in url for _, url, _, _ in calls)
    update_calls = [c for c in calls if c[0] == "POST"]
    assert len(update_calls) == 1
    assert update_calls[0][2]["key"] == "hashed-sauvage-key"
    assert update_calls[0][2]["models"] == ["tooling", "ornith-1.0", "qwen35-4b"]
