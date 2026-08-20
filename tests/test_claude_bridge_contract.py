"""Security and routing contract for the experimental Claude subscription bridge."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "k8s" / "claude-bridge.yaml"
MANIFEST = ROOT / "k8s" / "manifest.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "claude-bridge-image.yml"
ACCOUNTS = {"personal", "tercera", "works-shared"}
MODELS = {"opus", "sonnet", "haiku"}
PREFIX = "claude-test/"


def _documents(path):
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def _resource(documents, kind, name):
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_bridge_has_one_owner_and_persistent_isolated_account_homes():
    documents = _documents(BRIDGE)
    deployment = _resource(documents, "Deployment", "claude-subscription-bridge")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"

    pod = deployment["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["kubernetes.io/arch"] == "amd64"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["fsGroup"] == 1000

    claims = {
        volume["persistentVolumeClaim"]["claimName"]
        for volume in pod["volumes"]
        if "persistentVolumeClaim" in volume
    }
    assert claims == {f"claude-bridge-{account}" for account in ACCOUNTS}
    for account in ACCOUNTS:
        claim = _resource(documents, "PersistentVolumeClaim", f"claude-bridge-{account}")
        assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]

    container = pod["containers"][0]
    assert container["image"].startswith(
        "ghcr.io/pocharlies-org/claude-subscription-bridge:sha-"
    )
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["CLAUDE_BRIDGE_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "claude-bridge-api",
        "key": "api-key",
    }
    for account in ACCOUNTS:
        assert f'"id":"{account}"' in env["CLAUDE_BRIDGE_ACCOUNTS"]["value"]


def test_bridge_credential_is_generated_once_and_never_stored_in_git():
    documents = _documents(BRIDGE)
    generator = _resource(documents, "Password", "claude-bridge-api")
    assert generator["spec"]["length"] >= 32
    assert generator["spec"]["secretKeys"] == ["api-key"]
    external = _resource(documents, "ExternalSecret", "claude-bridge-api")
    assert external["spec"]["refreshPolicy"] == "CreatedOnce"
    assert external["spec"]["target"]["name"] == "claude-bridge-api"


def test_only_litellm_can_reach_the_authenticated_bridge():
    documents = _documents(BRIDGE)
    policy = _resource(documents, "NetworkPolicy", "claude-subscription-bridge")
    ingress = policy["spec"]["ingress"]
    assert ingress == [
        {
            "from": [{"podSelector": {"matchLabels": {"app": "litellm"}}}],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }
    ]
    assert set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}


def test_litellm_publishes_explicit_canaries_without_fallbacks_or_probes():
    documents = _documents(MANIFEST)
    config_map = _resource(documents, "ConfigMap", "litellm-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    entries = {
        item["model_name"]: item
        for item in config["model_list"]
        if item["model_name"].startswith(PREFIX)
    }
    expected = {
        f"{PREFIX}{account}/{model}" for account in ACCOUNTS for model in MODELS
    }
    assert set(entries) == expected
    for public_name, entry in entries.items():
        _, account, model = public_name.split("/")
        params = entry["litellm_params"]
        assert params["model"] == f"openai/{model}@{account}"
        assert params["api_base"] == (
            "http://claude-subscription-bridge.litellm.svc.cluster.local:8080/v1"
        )
        assert params["api_key"] == "os.environ/CLAUDE_BRIDGE_API_KEY"
        info = entry["model_info"]
        assert info["supports_function_calling"] is False
        assert info["disable_background_health_check"] is True

    fallbacks = config["router_settings"]["fallbacks"]
    serialized = yaml.safe_dump(fallbacks)
    assert PREFIX not in serialized


def test_litellm_reads_the_same_generated_bearer_credential():
    documents = _documents(MANIFEST)
    deployment = _resource(documents, "Deployment", "litellm")
    container = next(
        item for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "litellm"
    )
    env = {item["name"]: item for item in container["env"]}
    assert env["CLAUDE_BRIDGE_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "claude-bridge-api",
        "key": "api-key",
    }


def test_image_workflow_uses_arc_and_publishes_only_immutable_tags():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["build"]
    assert job["runs-on"] == "arc-k8s"
    commands = "\n".join(
        str(step.get("run", "")) for step in job["steps"]
    )
    assert 'tag="sha-${GITHUB_SHA::12}"' in commands
    assert ":latest" not in commands
    assert "Verify unauthenticated cluster pulls" in str(job["steps"])
    assert "repository%3Apocharlies-org%2Fclaude-subscription-bridge%3Apull" in commands
