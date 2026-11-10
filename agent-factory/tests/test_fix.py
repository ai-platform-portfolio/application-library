"""Safety and publication contracts for the separate fix worker phases."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from portfolio_agent_factory.fix import load_policy, propose, publish, validate, validate_proposal, write_json


SHA = "a" * 40


def policy():
    return {
        "repository": "ai-platform-portfolio/preview-environments",
        "base_ref": "fault/readiness",
        "base_sha": SHA,
        "incident_id": "AI-204",
        "editable_paths": ["deploy/", "tests/"],
        "checks": [["python", "-m", "json.tool", "deploy/values.json"]],
        "instructions": "Fix the readiness path only when the live evidence supports it.",
    }


def proposal():
    return {
        "schema_version": 1,
        "repository": policy()["repository"],
        "base_sha": SHA,
        "incident_id": "AI-204",
        "root_cause": "The configured readiness URL returns HTTP 404 while the actual health route returns 200.",
        "rationale": "Point readiness at the existing health route and retain a regression check.",
        "changes": [{"path": "deploy/values.json", "content": '{"readinessPath": "/api/healthz"}\n'}],
    }


class FakeResponses:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(status="completed", output=[SimpleNamespace(
                type="function_call", name="read_file", arguments='{"path":"deploy/values.json"}')])
        answer = {key: proposal()[key] for key in ("root_cause", "rationale", "changes")}
        return SimpleNamespace(status="completed", output=[], output_text=json.dumps(answer))


class FixTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "deploy").mkdir()
        (self.root / "deploy/values.json").write_text('{"readinessPath":"/api/missing-readyz"}\n')

    def test_policy_and_proposal_reject_scope_and_symlinks(self):
        path = self.root / "policy.json"
        write_json(path, policy())
        self.assertEqual(load_policy(path), policy())
        valid = proposal()
        validate_proposal(valid, policy(), self.root)
        valid["changes"][0]["path"] = "../.github/workflows/publish.yml"
        with self.assertRaises(ValueError):
            validate_proposal(valid, policy(), self.root)
        valid["changes"][0]["path"] = "deploy/link"
        (self.root / "deploy/link").symlink_to("/tmp/elsewhere")
        with self.assertRaises(ValueError):
            validate_proposal(valid, policy(), self.root)

    def test_model_reads_before_edit_and_validation_binds_exact_change(self):
        result = propose(policy(), self.root, {"answer": "Observed readiness 404", "trace_id": "t"},
                         SimpleNamespace(responses=FakeResponses()), "chat")
        self.assertEqual(result, proposal())
        report = validate(policy(), self.root, result)
        self.assertTrue(report["passed"])
        self.assertEqual(json.loads((self.root / "deploy/values.json").read_text())["readinessPath"], "/api/healthz")
        changed = proposal()
        changed["changes"][0]["content"] = '{"readinessPath":"/different"}'
        with self.assertRaises(ValueError):
            publish(policy(), self.root, changed, report, "test")

    def test_publisher_creates_only_draft_pr_from_exact_base(self):
        report = validate(policy(), self.root, proposal())
        seen = []

        def handle(request):
            seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
            path = request.url.path
            if path.endswith("/git/ref/heads/fault/readiness"):
                return httpx.Response(200, json={"object": {"sha": SHA}})
            if path.endswith("/pulls") and request.method == "GET":
                return httpx.Response(200, json=[])
            if path.endswith("/git/commits/" + SHA):
                return httpx.Response(200, json={"tree": {"sha": "b" * 40}})
            if path.endswith("/git/trees"):
                return httpx.Response(201, json={"sha": "c" * 40})
            if "/git/ref/heads/agent/" in path:
                return httpx.Response(404, json={"message": "Not Found"})
            if path.endswith("/git/commits"):
                return httpx.Response(201, json={"sha": "d" * 40})
            if path.endswith("/git/refs"):
                return httpx.Response(201, json={"ref": "created"})
            if path.endswith("/pulls"):
                return httpx.Response(201, json={"html_url": "https://github.com/ai-platform-portfolio/preview-environments/pull/42"})
            raise AssertionError(path)

        with httpx.Client(transport=httpx.MockTransport(handle), base_url="https://api.github.com") as client:
            result = publish(policy(), self.root, proposal(), report, "test", client=client)
        self.assertEqual(result["status"], "created_draft")
        pr = next(data for method, path, data in seen if method == "POST" and path.endswith("/pulls"))
        self.assertIs(pr["draft"], True)
        self.assertEqual(pr["base"], "fault/readiness")
        self.assertTrue(pr["head"].startswith("agent/ai-204-"))


if __name__ == "__main__":
    unittest.main()
