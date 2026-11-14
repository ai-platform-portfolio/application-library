"""Safety and publication contracts for the separate fix worker phases."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx

from portfolio_agent_runtime.coding import _run_check, load_policy, propose, publish, validate, validate_proposal, write_json
from portfolio_agent_runtime.handoff import prepare


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


class FakeCall(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


class FakeResponses:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls > 1:
            self.assert_tool_result(kwargs["input"])
        if self.calls <= 2:
            return SimpleNamespace(status="completed", output=[FakeCall(
                type="function_call", name="read_file", call_id=f"call-{self.calls}",
                arguments='{"path":"deploy/values.json"}')])
        answer = {key: proposal()[key] for key in ("root_cause", "rationale", "changes")}
        return SimpleNamespace(status="completed", output=[], output_text=json.dumps(answer))

    def assert_tool_result(self, history):
        assert history[-1]["type"] == "function_call_output"
        assert history[-1]["call_id"] == f"call-{self.calls - 1}"
        assert json.loads(history[-1]["output"])["path"] == "deploy/values.json"


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
        report = validate(policy(), self.root, result, sandbox_image=None)
        self.assertTrue(report["passed"])
        self.assertEqual(json.loads((self.root / "deploy/values.json").read_text())["readinessPath"], "/api/healthz")
        changed = proposal()
        changed["changes"][0]["content"] = '{"readinessPath":"/different"}'
        with self.assertRaises(ValueError):
            publish(policy(), self.root, changed, report, "test")

    def test_noisy_validator_fails_with_bounded_output_from_both_streams(self):
        configured = policy()
        configured['checks'] = [['python', '-c',
            "import os; os.write(1, b'x'*600000); os.write(2, b'y'*600000)"]]
        report = validate(configured, self.root, proposal(), sandbox_image=None)
        self.assertFalse(report['passed'])
        self.assertLessEqual(len(report['checks'][0]['output_tail']), 4096)
        self.assertIn('Validation output exceeds 1 MiB', report['checks'][0]['output_tail'])

    def test_timeout_fails_and_removes_the_named_container(self):
        with patch('portfolio_agent_runtime.coding.subprocess.run') as cleanup:
            code, output = _run_check(['python', '-c', 'import time; time.sleep(60)'],
                                      self.root, container='owned-test-container', timeout=0.2)
        self.assertNotEqual(code, 0)
        self.assertIn('Validation time budget exhausted', output)
        self.assertEqual(cleanup.call_args.args[0], ['docker', 'rm', '--force', 'owned-test-container'])

    def test_zero_test_failure_survives_output_tail_truncation(self):
        code, output = _run_check(['python', '-c',
            "import os; os.write(2, b'Ran 0 tests\\n'); os.write(1, b'x'*8192)", 'unittest'], self.root)
        self.assertNotEqual(code, 0)
        self.assertNotIn('Ran 0 tests', output)

    def test_handoff_uses_consumer_evidence_policy_without_kubernetes_names(self):
        configuration = {**policy(), 'context_paths': ['tests/'],
                         'required_evidence': {'source': 'live_http_probe', 'tools': ['service_status']}}
        config = self.root / 'coding.yaml'
        investigation = self.root / 'investigation.json'
        write_json(config, configuration)
        result = {'diagnosis': {'decision': 'actionable', 'answer': 'Observed HTTP failure'},
                  'evidence_source': 'live_http_probe', 'tool_calls': ['service_status'], 'trace_id': 'a' * 32}
        write_json(investigation, result)

        def source(_, root, token):
            (root / 'tests').mkdir(parents=True)
            (root / 'tests/probe.py').write_text('assert True\n')

        with patch('portfolio_agent_runtime.coding.github') as github, patch(
                'portfolio_agent_runtime.coding.checked', return_value={'object': {'sha': SHA}}), patch(
                'portfolio_agent_runtime.coding.fetch', side_effect=source):
            prepare(config, investigation, self.root / 'handoff', 'AI-204')
            request = json.loads((self.root / 'handoff/request.json').read_text())
            self.assertEqual(request['files'], {'tests/probe.py': 'assert True\n'})
            github.reset_mock()
            for override in ({'evidence_source': 'caller_provided'}, {'tool_calls': []}):
                write_json(investigation, {**result, **override})
                with self.assertRaises(ValueError):
                    prepare(config, investigation, self.root / 'rejected', 'AI-204')
                github.assert_not_called()
            configuration.pop('required_evidence')
            write_json(config, configuration)
            with self.assertRaises(KeyError):
                prepare(config, investigation, self.root / 'rejected', 'AI-204')
            github.assert_not_called()

    @unittest.skipUnless(os.getenv('VALIDATION_SANDBOX_IMAGE'), 'Docker sandbox acceptance is opt-in')
    def test_real_validation_container_has_no_credentials_network_or_host_write_access(self):
        self.root.chmod(0o755)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_readiness_contract.py').write_text("""
import os
from pathlib import Path
import socket
import unittest

class BoundaryTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(os.getuid(), 10001)
        self.assertFalse(any(k.startswith(('AZURE_', 'GITHUB_', 'ACTIONS_')) for k in os.environ))
        self.assertFalse(Path('/var/run/docker.sock').exists())
        with self.assertRaises(OSError):
            Path('/workspace/injected').write_text('no')
        with self.assertRaises(OSError):
            socket.create_connection(('1.1.1.1', 443), timeout=0.2)
""")
        configured = policy()
        configured['checks'] = [['python', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_readiness_contract.py']]
        report = validate(configured, self.root, proposal(), sandbox_image=os.environ['VALIDATION_SANDBOX_IMAGE'])
        self.assertTrue(report['passed'], report)
        self.assertFalse((self.root / 'injected').exists())

        # Exercise output abort through the real Docker path and verify the
        # long-running container is removed, not merely its client process.
        identity = uuid4()
        container = 'agent-validation-' + identity.hex
        self.addCleanup(subprocess.run, ['docker', 'rm', '--force', container],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        configured['checks'] = [['python', '-c',
            "import os,time; os.write(1,b'x'*1200000); time.sleep(60)"]]
        with patch('portfolio_agent_runtime.coding.uuid4', return_value=identity):
            report = validate(configured, self.root, proposal(), sandbox_image=os.environ['VALIDATION_SANDBOX_IMAGE'])
        self.assertFalse(report['passed'])
        self.assertIn('Validation output exceeds 1 MiB', report['checks'][0]['output_tail'])
        exists = subprocess.run(['docker', 'inspect', container], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=30)
        self.assertNotEqual(exists.returncode, 0, 'Aborted validation container is still present')

    def test_publisher_creates_only_draft_pr_from_exact_base(self):
        report = validate(policy(), self.root, proposal(), sandbox_image=None)
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
        self.assertEqual(pr["head"], "agent/ai-204")


if __name__ == "__main__":
    unittest.main()
