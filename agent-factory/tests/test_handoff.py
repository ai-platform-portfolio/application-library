"""Focused checks for the alert-to-coding security and lifecycle boundary."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from portfolio_agent_factory.broker import degraded_readiness, triage_answer
from portfolio_agent_factory.handoff import Kubernetes, checked_result, dispatch, render_fix, resolve_sha


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "example.azurecr.io/portfolio/agent@sha256:" + "a" * 64
TRACE = "f" * 32
INTENT = {
    "agent": {"id": "sre", "image": IMAGE, "revision": "b" * 40},
    "target": {"namespace": "preview-test", "workload": "web"},
    "broker_service_account": "foundry-diagnostic-broker",
    "github": {"appId": "5471830", "installationId": "175270905"},
    "policy": {
        "repository": "ai-platform-portfolio/preview-environments",
        "base_ref": "fault/readiness",
        "editable_paths": ["deploy/", "tests/"],
        "checks": [["python", "-m", "unittest", "discover", "-s", "tests"]],
        "instructions": "Compare the readiness route with the configured probe and add a regression test.",
    },
    "publish": True,
    "required_evidence": ["workload_state", "recent_events"],
}
RESULT = {"decision": "actionable", "answer": "The configured readiness route returned 404 while the app route returned 200.",
          "evidence": ["workload_state", "recent_events"]}


def artifact(result=RESULT):
    document = {"agent": "sre", "version": "2", "target": INTENT["target"],
                "answer": result["answer"], "triage": result,
                "tool_calls": ["workload_state", "recent_events"], "model_requests": 3,
                "trace_id": TRACE, "broker_trace_id": TRACE}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"))
    marker = json.dumps({"decision": result["decision"], "trace_id": TRACE,
                         "sha256": hashlib.sha256(raw.encode()).hexdigest()})
    return raw, marker


class HandoffTests(unittest.TestCase):
    def test_broker_requires_structured_cited_diagnosis(self):
        self.assertEqual(triage_answer(json.dumps(RESULT),
                                       ["workload_state", "recent_events"], ["workload_state", "recent_events"]),
                         RESULT)
        with self.assertRaises(ValueError):
            triage_answer("Readiness looks broken", ["workload_state", "recent_events"], ["workload_state"])
        with self.assertRaises(ValueError):
            triage_answer(json.dumps({"decision": "actionable", "answer": RESULT["answer"],
                                      "evidence": ["workload_state"]}), ["workload_state"], ["recent_events"])
        with self.assertRaises(ValueError):
            raw, marker = artifact()
            checked_result(raw.replace("preview-test", "attacker"), marker, INTENT)

    def test_readiness_gate_requires_current_degradation_and_matching_event(self):
        history = [
            {"name": "workload_state", "result": {"deployment": {"desired": 1, "ready": 0}}},
            {"name": "recent_events", "result": {"events": [
                {"reason": "Unhealthy", "message": "Readiness probe failed: HTTP 404"}
            ]}},
        ]
        self.assertTrue(degraded_readiness(history))
        history[0]["result"]["deployment"]["ready"] = 1
        self.assertFalse(degraded_readiness(history))
        history[0]["result"]["deployment"]["ready"] = 0
        history[1]["result"]["events"] = []
        self.assertFalse(degraded_readiness(history))

    def test_abstention_never_resolves_source_or_creates_resources(self):
        api = Mock()
        raw, marker = artifact(RESULT | {"decision": "abstain"})
        with patch("portfolio_agent_factory.handoff.resolve_sha") as source:
            outcome = dispatch(INTENT, raw, marker,
                               "diag-sre-incident", "handoff-sre-incident", "read-token", api=api)
        self.assertEqual(outcome["status"], "abstained")
        source.assert_not_called()
        api.create.assert_not_called()

    def test_source_ref_resolves_to_full_sha(self):
        response = Mock()
        response.json.return_value = {"sha": "c" * 40}
        client = Mock()
        client.get.return_value = response
        self.assertEqual(resolve_sha("owner/repo", "fault/readiness", "read-token", client=client), "c" * 40)
        self.assertIn("fault%2Freadiness", client.get.call_args.args[0])

    def test_duplicate_create_reuses_only_matching_resource(self):
        client = Mock()
        client.post.return_value.status_code = 409
        client.get.return_value.json.return_value = {
            "spec": {"repositories": ["preview-environments"], "defaulted": True}
        }
        api = Kubernetes(client=client)
        resource = {"kind": "GithubAccessToken", "metadata": {"name": "token", "namespace": "argocd"},
                    "spec": {"repositories": ["preview-environments"]}}
        api.create(resource)
        client.get.return_value.json.return_value = {"spec": {"repositories": ["other-repo"]}}
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            api.create(resource)
        resource["metadata"]["ownerReferences"] = [{"apiVersion": "batch/v1", "kind": "Job",
                                                     "name": "handoff", "uid": "owner"}]
        client.get.return_value.json.return_value = {"metadata": {"ownerReferences": []},
                                                      "spec": {"repositories": ["preview-environments"]}}
        with self.assertRaisesRegex(ValueError, "owner"):
            api.create(resource)

    def test_dispatcher_rejects_spoofed_diagnostic_pod(self):
        client = Mock()
        client.get.side_effect = [
            Mock(json=Mock(return_value={"metadata": {"ownerReferences": [{"kind": "Job", "name": "other"}]},
                                         "status": {"phase": "Succeeded"}})),
            Mock(json=Mock(return_value={"metadata": {"uid": "job-uid"}})),
        ]
        with self.assertRaisesRegex(ValueError, "not owned"):
            Kubernetes(client=client).read_log("diag-sre-pod", "diag-sre-incident", INTENT)

    def test_shared_chart_renders_scoped_publisher(self):
        docs = render_fix(INTENT, RESULT | {"trace_id": TRACE}, "diag-sre-incident", "c" * 40)
        self.assertEqual(len(docs), 6)
        token = [item for item in docs if item["kind"] == "GithubAccessToken"]
        self.assertEqual({item["spec"]["permissions"]["contents"] for item in token}, {"read", "write"})
        job = next(item for item in docs if item["kind"] == "Job")
        self.assertEqual(job["spec"]["template"]["spec"]["serviceAccountName"], "foundry-coding-worker")

    def test_dispatch_cleans_tokens_after_terminal_worker_and_keeps_job_logs(self):
        api = Mock()
        api.wait_job.return_value = True
        api.handoff_owner.return_value = {"apiVersion": "batch/v1", "kind": "Job", "name": "handoff", "uid": "owner"}
        api.reference.return_value = {"apiVersion": "batch/v1", "kind": "Job", "name": "fix", "uid": "worker"}
        raw, marker = artifact()
        with patch("portfolio_agent_factory.handoff.resolve_sha", return_value="c" * 40), \
             patch("portfolio_agent_factory.handoff.render_fix", return_value=[
                 {"kind": "GithubAccessToken", "metadata": {"name": "token"}},
                 {"kind": "ConfigMap", "metadata": {"name": "input"}},
                 {"kind": "Job", "metadata": {"name": "fix"}},
             ]):
            outcome = dispatch(INTENT, raw, marker, "diag-sre-incident", "handoff-sre-incident", "read-token", api=api)
        self.assertTrue(outcome["published"])
        self.assertEqual([call.args[0]["kind"] for call in api.delete.call_args_list],
                         ["ConfigMap", "GithubAccessToken"])
        created = [call.args[0] for call in api.create.call_args_list]
        self.assertEqual([item["kind"] for item in created], ["GithubAccessToken", "Job", "ConfigMap"])
        self.assertEqual(created[0]["metadata"]["ownerReferences"][0]["uid"], "owner")
        self.assertEqual(created[-1]["metadata"]["ownerReferences"][0]["uid"], "worker")

    def test_alert_chart_passes_successful_pod_result_to_separate_job(self):
        values = {
            "agent": {"id": "sre", "image": IMAGE, "revision": "b" * 40, "version": "2"},
            "target": {"namespace": "preview-test", "workload": "web"},
            "handoff": {"enabled": True, "publish": True,
                        "requiredEvidence": ["workload_state", "recent_events"]},
            "github": INTENT["github"], "policy": INTENT["policy"],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "values.yaml"
            path.write_text(yaml.safe_dump(values))
            rendered = subprocess.run(["helm", "template", "sre-alerts", str(ROOT / "alert-chart"),
                                       "--namespace", "cluster-onboarding", "--values", str(path)],
                                      capture_output=True, text=True, check=True)
        docs = [item for item in yaml.safe_load_all(rendered.stdout) if item]
        name = "agent-alert-sre-" + hashlib.sha256(b"preview-test/web").hexdigest()[:8]
        source = next(item for item in docs if item["kind"] == "EventSource")
        self.assertEqual(set(source["spec"]["resource"]), {"readiness", "diagnostic"})
        sensor = next(item for item in docs if item["kind"] == "Sensor" and item["metadata"]["name"] == name + "-handoff")
        filters = sensor["spec"]["dependencies"][0]["filters"]["data"]
        self.assertIn("body.status.phase", {item["path"] for item in filters})
        job = sensor["spec"]["triggers"][0]["template"]["k8s"]["source"]["resource"]
        self.assertNotEqual(job["spec"]["template"]["spec"]["serviceAccountName"], "foundry-diagnostic-broker")
        self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["image"], IMAGE)


if __name__ == "__main__":
    unittest.main()
