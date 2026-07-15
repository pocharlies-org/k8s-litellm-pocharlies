import ast
from pathlib import Path
from textwrap import dedent

import pytest
import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"
ORPHAN_ID = "793b8450-a1f9-4f18-991f-e70c5a665b30"


def load_migration(delete_model):
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    config_map = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == "litellm-dgx-backend-sync"
    )
    module = ast.parse(dedent(config_map["data"]["sync.py"]))
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "purge_manual_alias_orphan"
    )
    namespace = {
        "Any": object,
        "MANUAL_ALIAS_ORPHAN_ID": ORPHAN_ID,
        "MANUAL_ALIAS_ORPHAN_EXPECTED_ALIAS": "compaction-local",
        "MANUAL_ALIAS_ORPHAN_EXPECTED_MANAGER": "manual-alias-revive",
        "current_model_rows": lambda: [],
        "delete_model": delete_model,
        "log": type("Log", (), {"info": staticmethod(lambda *args: None)})(),
    }
    exec(
        compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "sync.py", "exec"),
        namespace,
    )
    return namespace["purge_manual_alias_orphan"]


def orphan_row(alias="compaction-local", manager="manual-alias-revive"):
    return {
        "model_name": alias,
        "model_info": {"id": ORPHAN_ID, "managed_by": manager},
    }


def test_exact_manual_orphan_is_purged_once_and_absence_is_idempotent():
    deleted = []
    purge = load_migration(deleted.append)

    assert purge([orphan_row()]) is True
    assert deleted == [ORPHAN_ID]
    assert purge([]) is False
    assert deleted == [ORPHAN_ID]


@pytest.mark.parametrize(
    "row",
    [
        orphan_row(alias="tooling"),
        orphan_row(manager="litellm-dgx-backend-sync"),
    ],
)
def test_orphan_migration_fails_closed_on_alias_or_owner_mismatch(row):
    deleted = []
    purge = load_migration(deleted.append)

    with pytest.raises(RuntimeError, match="ownership/alias contract mismatch"):
        purge([row])
    assert deleted == []


def test_orphan_migration_fails_closed_if_uuid_is_not_unique():
    deleted = []
    purge = load_migration(deleted.append)

    with pytest.raises(RuntimeError, match="is not unique"):
        purge([orphan_row(), orphan_row()])
    assert deleted == []


def test_migration_runs_before_controller_reconciliation_loop():
    docs = [doc for doc in yaml.safe_load_all(MANIFEST.read_text()) if doc]
    config_map = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap"
        and doc.get("metadata", {}).get("name") == "litellm-dgx-backend-sync"
    )
    text = config_map["data"]["sync.py"]
    main = text[text.index("def main() -> None:"):text.index('if __name__ == "__main__":')]

    assert main.index("purge_manual_alias_orphan()") < main.index("while True:")
