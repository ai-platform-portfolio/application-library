"""Render the alert bridge that launches the existing read-only broker."""

import subprocess
import unittest
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[1] / "alert-chart"
IMAGE = "example.azurecr.io/portfolio/agent@sha256:" + "a" * 64
REVISION = "b" * 40


def render(*extra):
    command = [
        "helm", "template", "sre-alerts", str(CHART), "--namespace", "cluster-onboarding",
        "--set", "agent.id=sre", "--set", f"agent.image={IMAGE}",
        "--set", f"agent.revision={REVISION}", "--set", "agent.version=1",
        "--set", "target.namespace=preview-test", "--set", "target.workload=web",
        *extra,
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


class AlertChartTests(unittest.TestCase):
    def test_scoped_event_creates_bounded_read_only_job(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = {(item["kind"], item["metadata"]["name"]): item
                for item in yaml.safe_load_all(result.stdout) if item}

        source = docs["EventSource", "agent-alert-sre"]["spec"]
        self.assertEqual(source["resource"]["readiness"]["namespace"], "preview-test")
        self.assertEqual(source["resource"]["readiness"]["resource"], "events")

        sensor = docs["Sensor", "agent-alert-sre"]["spec"]
        filters = sensor["dependencies"][0]["filters"]["data"]
        self.assertIn("body.message", {item["path"] for item in filters})
        trigger = sensor["triggers"][0]
        self.assertEqual(trigger["template"]["k8s"]["parameters"][0]["dest"], "metadata.name")
        self.assertEqual(trigger["rateLimit"]["requestsPerUnit"], 2)
        job = trigger["template"]["k8s"]["source"]["resource"]
        self.assertEqual(job["metadata"]["namespace"], "foundry-publisher")
        self.assertEqual(job["spec"]["template"]["spec"]["serviceAccountName"], "foundry-diagnostic-broker")
        self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["image"], IMAGE)
        self.assertEqual(job["spec"]["ttlSecondsAfterFinished"], 86400)
        incident = docs["ConfigMap", "agent-alert-sre-incident"]["data"]["incident.json"]
        self.assertNotIn("{{", incident)
        self.assertIn('"workload":"web"', incident)

        roles = [item for (kind, _), item in docs.items() if kind == "Role"]
        self.assertEqual({item["metadata"]["namespace"] for item in roles},
                         {"preview-test", "foundry-publisher"})

    def test_rejects_unpinned_image(self):
        result = render("--set", "agent.image=example.azurecr.io/portfolio/agent:latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest-pinned", result.stderr)

    def test_registration_renders_only_child_application(self):
        result = render("--set", "registration.enabled=true",
                        "--set", "registration.repository=https://github.com/ai-platform-portfolio/application-library.git",
                        "--set", "registration.revision=" + "c" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [item for item in yaml.safe_load_all(result.stdout) if item]
        self.assertEqual([item["kind"] for item in docs], ["Application"])
        app = docs[0]
        self.assertEqual(app["metadata"]["namespace"], "argocd")
        self.assertFalse(app["spec"]["source"]["helm"]["valuesObject"]["registration"]["enabled"])
        self.assertEqual(app["spec"]["destination"]["namespace"], "cluster-onboarding")


if __name__ == "__main__":
    unittest.main()
