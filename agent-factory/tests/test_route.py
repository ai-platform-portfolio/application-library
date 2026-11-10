import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import yaml

from portfolio_agent_factory.release import change_route, validate_route_evidence


IMAGE = "registry.example.test/agents/catalog@sha256:" + "a" * 64
RUNNER = "registry.example.test/agents/catalog@sha256:" + "c" * 64
REVISION = "b" * 40
CHART = Path(__file__).resolve().parents[1] / "chart"
ENDPOINT = "https://foundry.example.test/api/projects/management"


def request():
    return {"agent_name": "sre", "draft": False,
            "metadata": {"image_digest": IMAGE, "source_revision": REVISION}}


def evidence(mode="promote", **changes):
    value = {"agent": "sre", "base_version": "1", "target_version": "2",
             "image_digest": IMAGE, "source_revision": REVISION,
             "review": {"status": "approved", "url": "https://github.com/example/repo/pull/2"}}
    if mode == "promote":
        value["evaluation"] = {"status": "human_review_required", "human_review": "pending",
                               "automated_checks": {name: True for name in (
                                   "declared_tools_only", "within_tool_limit", "within_request_limit",
                                   "required_tools_used", "forbidden_tools_avoided",
                                   "forbidden_answer_patterns_absent")}}
    else:
        value["reason"] = "Revert the failed candidate"
    value.update(changes)
    return value


def response(route, *, etag=None):
    return SimpleNamespace(
        json=lambda: {"agent_endpoint": {
            "version_selector": {"version_selection_rules": [
                {"agent_version": route, "traffic_percentage": 100, "type": "FixedRatio"}]},
            "protocol_configuration": {"invocations": {}, "responses": {}},
            "authorization_schemes": [{"type": "AzureRbac"}],
        }},
        headers={"ETag": etag} if etag else {},
        raise_for_status=lambda: None,
    )


class RouteTests(unittest.TestCase):
    def route_clients(self, before="1", after="2", *, etag='"v1"'):
        sender = Mock()
        sender.get.side_effect = [response(before, etag=etag), response(after)]
        sender.patch.return_value.raise_for_status.return_value = None
        project = Mock()
        project.agents.get_version.return_value = SimpleNamespace(
            status="active", metadata={"image_digest": IMAGE, "source_revision": REVISION})
        credential = Mock()
        credential.get_token.return_value.token = "token"
        return sender, project, credential

    def test_promote_patches_only_selector_and_verifies_readback(self):
        sender, project, credential = self.route_clients()
        result = change_route(ENDPOINT, request(), "1", "2", evidence(), "promote",
                              credential, client=sender, project=project)
        self.assertEqual(result["status"], "verified")
        patch = sender.patch.call_args
        self.assertEqual(patch.kwargs["headers"]["If-Match"], '"v1"')
        self.assertEqual(patch.kwargs["json"], {"agent_endpoint": {"version_selector": {
            "version_selection_rules": [{"agent_version": "2", "traffic_percentage": 100,
                                         "type": "FixedRatio"}]}}})
        self.assertNotIn("protocol_configuration", json.dumps(patch.kwargs["json"]))
        project.agents.get_version.assert_called_once_with(agent_name="sre", agent_version="2")

    def test_refuses_stale_route_wrong_metadata_and_unreviewed_evidence(self):
        sender, project, credential = self.route_clients(before="3")
        with self.assertRaisesRegex(ValueError, "differs from the expected base"):
            change_route(ENDPOINT, request(), "1", "2", evidence(), "promote",
                         credential, client=sender, project=project)
        sender.patch.assert_not_called()

        sender, project, credential = self.route_clients()
        project.agents.get_version.return_value.metadata["image_digest"] = RUNNER
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            change_route(ENDPOINT, request(), "1", "2", evidence(), "promote",
                         credential, client=sender, project=project)
        sender.get.assert_not_called()

        with self.assertRaisesRegex(ValueError, "passing factory evaluation"):
            validate_route_evidence(evidence(evaluation={"automated_checks": {"safe": True},
                                                 "human_review": "pending"}),
                                    request(), "1", "2", "promote")
        with self.assertRaisesRegex(ValueError, "reviewed HTTPS approval"):
            validate_route_evidence(evidence(review={"status": "pending", "url": "https://example.test/review"}),
                                    request(), "1", "2", "promote")

    def test_rollback_and_idempotent_retry(self):
        sender, project, credential = self.route_clients()
        result = change_route(ENDPOINT, request(), "1", "2", evidence("rollback"), "rollback",
                              credential, client=sender, project=project)
        self.assertEqual(result["status"], "verified")
        sender, project, credential = self.route_clients(before="2")
        result = change_route(ENDPOINT, request(), "1", "2", evidence(), "promote",
                              credential, client=sender, project=project)
        self.assertEqual(result["status"], "already_target")
        sender.patch.assert_not_called()

    def test_readback_catches_unexpected_endpoint_change(self):
        sender, project, credential = self.route_clients(after="1")
        with self.assertRaisesRegex(RuntimeError, "readback"):
            change_route(ENDPOINT, request(), "1", "2", evidence(), "promote",
                         credential, client=sender, project=project)

    def render(self, mode, release=None, publisher_image=RUNNER, review=None):
        values = {"agent": {"id": "sre", "image": IMAGE, "revision": REVISION},
                  "release": {"mode": mode, "baseVersion": "1", "targetVersion": "2",
                              "evidence": review if review is not None else evidence(mode)},
                  "publisher": {"image": publisher_image}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.json"
            path.write_text(json.dumps(values))
            return subprocess.run(["helm", "template", release or "sre-route", str(CHART), "-f", str(path)],
                                  capture_output=True, text=True,
                                  env={**os.environ, "HELM_PLUGINS": directory})

    def test_chart_separates_runner_from_target_and_mounts_review(self):
        rendered = self.render("rollback")
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        docs = {doc["kind"]: doc for doc in yaml.safe_load_all(rendered.stdout)}
        job = docs["Job"]
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], RUNNER)
        self.assertIn(IMAGE, container["args"])
        self.assertIn("rollback", container["args"])
        self.assertEqual(job["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"],
                         docs["ConfigMap"]["metadata"]["name"])
        self.assertEqual(json.loads(docs["ConfigMap"]["data"]["evidence.json"]), evidence("rollback"))
        self.assertEqual(job["metadata"]["name"], next(doc["metadata"]["name"] for doc in
                         yaml.safe_load_all(self.render("rollback").stdout) if doc["kind"] == "Job"))

        for mode in ("first", "candidate"):
            result = self.render(mode, publisher_image="")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([doc["kind"] for doc in yaml.safe_load_all(result.stdout)], ["Job"])

    def test_chart_rejects_missing_runner_and_unbounded_evidence(self):
        missing = self.render("promote", publisher_image="")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("publisher.image is required", missing.stderr)
        oversized = self.render("promote", review=evidence(note="x" * 8192))
        self.assertNotEqual(oversized.returncode, 0)
        self.assertIn("8192 bytes", oversized.stderr)

    def test_cli_validates_review_without_applying(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = Path(directory) / "sre.yaml"
            agent.write_text(yaml.safe_dump({
                "schema_version": 1, "id": "sre", "model": "chat", "handler": "sample:run",
                "eval_suite": "test", "reasoning_effort": "medium", "instructions": "Investigate",
                "limits": {"model_requests": 3, "tool_calls": 0},
            }))
            review = Path(directory) / "evidence.json"
            review.write_text(json.dumps(evidence()))
            result = subprocess.run([
                sys.executable, "-m", "portfolio_agent_factory", "promote",
                "--agent", str(agent), "--image", IMAGE, "--revision", REVISION,
                "--base-version", "1", "--target-version", "2", "--evidence", str(review),
            ], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "validated_only")


if __name__ == "__main__":
    unittest.main()
