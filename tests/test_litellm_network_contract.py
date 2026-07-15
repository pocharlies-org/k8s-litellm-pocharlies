import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LOCK = ROOT / ".github" / "requirements" / "litellm-network-contract.txt"

CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SETUP_PYTHON_ACTION = (
    "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
)
PYYAML_HASH = "80bab7bfc629882493af4aa31a4cfa43a4c57c83813253626916b8c7ada83476"


class TestLiteLLMNetworkContract(unittest.TestCase):
    def resource(self, kind, name):
        matches = [
            document
            for document in yaml.safe_load_all(MANIFEST.read_text())
            if document
            and document.get("kind") == kind
            and document.get("metadata", {}).get("name") == name
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_litellm_uses_service_networking_without_a_host_port(self):
        deployment = self.resource("Deployment", "litellm")
        pod_template = deployment["spec"]["template"]
        pod_spec = pod_template["spec"]
        containers = pod_spec["containers"]
        litellm = next(
            container for container in containers if container["name"] == "litellm"
        )

        self.assertFalse(pod_spec.get("hostNetwork", False))
        self.assertIn({"name": "http", "containerPort": 4000}, litellm["ports"])
        self.assertTrue(
            all(
                "hostPort" not in port
                for container in containers
                for port in container.get("ports", [])
            )
        )

        service = self.resource("Service", "litellm")
        service_spec = service["spec"]
        deployment_selector = deployment["spec"]["selector"]["matchLabels"]
        pod_labels = pod_template["metadata"]["labels"]

        self.assertEqual(deployment_selector, pod_labels)
        self.assertEqual(service_spec["selector"], pod_labels)
        self.assertEqual(service_spec.get("type", "ClusterIP"), "ClusterIP")
        self.assertNotEqual(service_spec.get("clusterIP"), "None")
        self.assertNotIn("externalIPs", service_spec)
        self.assertNotIn("externalName", service_spec)
        self.assertNotIn("loadBalancerIP", service_spec)
        self.assertTrue(
            all("nodePort" not in port for port in service_spec["ports"])
        )

        http_port = next(
            port for port in service_spec["ports"] if port["name"] == "http"
        )
        self.assertEqual(http_port["port"], 4000)
        self.assertEqual(http_port["targetPort"], 4000)

    def test_ci_job_is_pinned_and_uses_the_minimal_lock(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        job = workflow["jobs"]["litellm-network-contract"]
        steps = job["steps"]

        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        action_uses = [step["uses"] for step in steps if "uses" in step]
        self.assertEqual(action_uses, [CHECKOUT_ACTION, SETUP_PYTHON_ACTION])
        for action in action_uses:
            reference = action.rsplit("@", 1)[1]
            self.assertRegex(reference, re.compile(r"^[0-9a-f]{40}$"))

        setup_python = next(
            step for step in steps if step.get("uses") == SETUP_PYTHON_ACTION
        )
        self.assertEqual(setup_python["with"]["python-version"], "3.12")

        run_commands = [step["run"] for step in steps if "run" in step]
        self.assertIn('test "$(uname -m)" = "x86_64"', run_commands)
        self.assertIn(
            "python -m pip install --require-hashes --only-binary=PyYAML "
            "-r .github/requirements/litellm-network-contract.txt",
            run_commands,
        )
        self.assertIn(
            "python -m unittest tests/test_litellm_network_contract.py",
            run_commands,
        )

        lock_text = LOCK.read_text()
        self.assertEqual(
            lock_text.splitlines(),
            ["PyYAML==6.0.2 \\", f"    --hash=sha256:{PYYAML_HASH}"],
        )
        self.assertTrue(lock_text.endswith("\n"))
        self.assertNotRegex(lock_text, re.compile(r">=|<=|~=|!=|\*|(?:^|\n)\s*-r"))


if __name__ == "__main__":
    unittest.main()
