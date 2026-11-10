"""Render and check the token and execution boundaries of the fix Job."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


CHART = Path(__file__).resolve().parents[1] / "fix-chart"
IMAGE = "example.azurecr.io/portfolio/agent@sha256:" + "a" * 64


def render(publish=False):
    values = {
        "agent": {"id": "sre", "image": IMAGE, "revision": "b" * 40},
        "github": {"appId": "5471830", "installationId": "175270905"},
        "policy": {"repository": "ai-platform-portfolio/preview-environments", "base_ref": "fault/readiness",
                   "base_sha": "c" * 40, "incident_id": "AI-204", "editable_paths": ["deploy/", "tests/"],
                   "checks": [["python", "-m", "unittest", "discover", "-s", "tests"]],
                   "instructions": "Check the real health route and add a regression test."},
        "diagnosis": {"answer": "The configured readiness URL returned 404, while the application route returned 200.",
                      "trace_id": "test-trace"},
        "publish": {"enabled": publish},
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.yaml"
        path.write_text(yaml.safe_dump(values))
        result = subprocess.run(["helm", "template", "fix", str(CHART), "--namespace", "foundry-publisher",
                                 "--values", str(path)], capture_output=True, text=True)
    return result


class FixChartTests(unittest.TestCase):
    def test_read_only_mode_does_not_request_write_token(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [item for item in yaml.safe_load_all(result.stdout) if item]
        tokens = [item for item in docs if item["kind"] == "GithubAccessToken"]
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["spec"]["repositories"], ["preview-environments"])
        self.assertEqual(tokens[0]["spec"]["permissions"], {"contents": "read"})
        job = next(item for item in docs if item["kind"] == "Job")
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual([item["name"] for item in pod["initContainers"]], ["fetch", "propose", "validate"])
        self.assertEqual([item["name"] for item in pod["containers"]], ["publish"])
        self.assertNotIn("GITHUB_WRITE_TOKEN", str(pod))

    def test_publish_token_only_reaches_final_container(self):
        result = render(publish=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = [item for item in yaml.safe_load_all(result.stdout) if item]
        tokens = [item for item in docs if item["kind"] == "GithubAccessToken"]
        self.assertEqual(len(tokens), 2)
        write = next(item for item in tokens if item["metadata"]["name"].endswith("-write"))
        self.assertEqual(write["spec"]["permissions"], {"contents": "write", "pull_requests": "write"})
        pod = next(item for item in docs if item["kind"] == "Job")["spec"]["template"]["spec"]
        self.assertNotIn("GITHUB_WRITE_TOKEN", str(pod["initContainers"]))
        self.assertIn("GITHUB_WRITE_TOKEN", str(pod["containers"][0]["env"]))
        self.assertIn("--apply", pod["containers"][0]["args"])


if __name__ == "__main__":
    unittest.main()
