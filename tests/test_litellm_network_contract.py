import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "k8s" / "manifest.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
LOCK = ROOT / ".github" / "requirements" / "litellm-contracts.txt"

CHECKOUT_ACTION = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SETUP_PYTHON_ACTION = (
    "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
)


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

    def resources(self, kind):
        return [
            document
            for document in yaml.safe_load_all(MANIFEST.read_text())
            if document and document.get("kind") == kind
        ]

    def test_litellm_repo_only_owns_the_legacy_lan_alias(self):
        """Canonical split routing is owned centrally by k8s-infra."""
        public = [
            resource
            for resource in self.resources("IngressRoute")
            if resource.get("metadata", {}).get("name") == "litellm-public"
        ]
        self.assertEqual(public, [], "the app repo must not duplicate central routing")

        lan = self.resource("IngressRoute", "litellm-lan")
        matches = [route["match"] for route in lan["spec"]["routes"]]
        self.assertEqual(matches, ["Host(`litellm.lan.e-dani.com`)"])
        self.assertNotIn("traefik-edge", lan["spec"].get("ingressClassName", ""))

    def test_every_litellm_request_timeout_is_600_seconds(self):
        config = self.resource("ConfigMap", "litellm-config")["data"]["config.yaml"]
        parsed = yaml.safe_load(config)
        self.assertEqual(parsed["litellm_settings"]["request_timeout"], 600)
        self.assertEqual(parsed["router_settings"]["timeout"], 600)
        model_list = parsed["model_list"]
        self.assertGreater(len(model_list), 0)
        explicit_timeouts = {
            entry["litellm_params"]["timeout"]
            for entry in model_list
            if "timeout" in entry.get("litellm_params", {})
        }
        self.assertEqual(
            explicit_timeouts,
            {600},
        )

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

    def test_ci_job_is_pinned_and_runs_the_whole_contract_suite(self):
        """El job sigue pineado por SHA, y ahora corre la suite entera.

        Reescrito 2026-08-10. Este test exigia el job `litellm-network-contract`,
        que instalaba solo PyYAML y corria UN fichero por unittest. Todo lo que usa
        fixtures de pytest quedaba sin ejecutar, o sea el resto de los contratos:
        asi es como tres de ellos estuvieron rojos desde el corte a DeepSeek sin que
        nadie lo viera, incluido uno que asertaba 4 backends habiendo ya 5.

        Lo que este test protege sigue siendo lo mismo — runner fijo, acciones
        pineadas por SHA de 40 hex, deps con --require-hashes — solo cambia el
        alcance de lo que se ejecuta.

        2026-08-13: el runner fijo pasa de ubuntu-24.04 a arc-k8s. Lo que se
        protege aqui es que este PINEADO, no que sea de GitHub: arc-k8s es el
        pool propio de la org, con nodeSelector kubernetes.io/arch=amd64, asi
        que el guard de x86_64 de mas abajo sigue valiendo. No hay motivo de red
        en esta eleccion — los contratos de este fichero se leen de los YAML del
        repo, no salen a la red.
        """
        workflow = yaml.safe_load(WORKFLOW.read_text())
        job = workflow["jobs"]["litellm-contracts"]
        steps = job["steps"]

        self.assertEqual(job["runs-on"], "arc-k8s")
        self.assertEqual(job["timeout-minutes"], 10)
        checkout = next(step for step in steps if step.get("uses") == CHECKOUT_ACTION)
        self.assertEqual(checkout["with"], {"persist-credentials": False})
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
            "python -m pip install --require-hashes --only-binary=:all: "
            "-r .github/requirements/litellm-contracts.txt",
            run_commands,
        )
        # La suite ENTERA, no un fichero. Un `pytest <ruta concreta>` aqui volveria
        # a dejar contratos sin ejecutar.
        self.assertIn("python -m pytest tests/ -q", run_commands)

        # El lock: lo que importa es que TODO paquete este clavado a una version
        # exacta con su sha256, no cual es cual. Antes se asertaba el contenido
        # literal (dos lineas, solo PyYAML), que es lo que hacia imposible anadir
        # pytest sin tocar el test -- y sin pytest la suite no se podia correr.
        lock_text = LOCK.read_text()
        paquetes = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^ \\]+) \\\n\s+--hash=sha256:([0-9a-f]{64})$",
                              lock_text)
        self.assertIn("pytest", [n for n, _, _ in paquetes],
                      "sin pytest en el lock el job no puede correr la suite")
        self.assertIn("PyYAML", [n for n, _, _ in paquetes])
        # Cada entrada `nombre==version` del fichero tiene que haber encajado con el
        # patron de arriba, es decir llevar su hash pegado.
        self.assertEqual(len(paquetes), len(re.findall(r"(?m)^[A-Za-z0-9_.-]+==", lock_text)))
        self.assertTrue(lock_text.endswith("\n"))
        # Ni rangos, ni comodines, ni requirements anidados: --require-hashes lo
        # rechazaria, pero mejor fallar aqui que en CI.
        self.assertNotRegex(lock_text, re.compile(r">=|<=|~=|!=|\*|(?:^|\n)\s*-r"))


if __name__ == "__main__":
    unittest.main()
