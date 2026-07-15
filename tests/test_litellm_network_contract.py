from pathlib import Path

import yaml


MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "manifest.yaml"


def resource(kind, name):
    matches = [
        document
        for document in yaml.safe_load_all(MANIFEST.read_text())
        if document
        and document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_litellm_uses_service_networking_without_a_host_port():
    deployment = resource("Deployment", "litellm")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    litellm = next(container for container in containers if container["name"] == "litellm")

    assert {"name": "http", "containerPort": 4000} in litellm["ports"]
    assert all(
        "hostPort" not in port
        for container in containers
        for port in container.get("ports", [])
    )

    service = resource("Service", "litellm")
    http_port = next(port for port in service["spec"]["ports"] if port["name"] == "http")
    assert http_port["port"] == 4000
    assert http_port["targetPort"] == 4000
