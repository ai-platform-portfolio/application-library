import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portfolio_agent_factory.manifest import load_agent
from portfolio_agent_factory.publish import create_candidate_release, create_draft, create_first_release, version_request
from portfolio_agent_factory.model import model_client
from portfolio_agent_factory.release import candidate_and_smoke, require_pinned_route, smoke, wait_active
from portfolio_agent_factory.broker import run_broker
from portfolio_agent_factory.diagnostics import KubernetesDiagnostics, redact


IMAGE = "registry.example.test/agents/catalog@sha256:" + "a" * 64
REVISION = "b" * 40


def agent_file(directory, name):
    path = Path(directory) / f"{name}.yaml"
    path.write_text(f"""schema_version: 1
id: {name}
model: chat
handler: sample_agent:handle
eval_suite: smoke
reasoning_effort: medium
instructions: Investigate the input.
limits:
  model_requests: 4
  tool_calls: 0
""")
    return path


class FactoryTests(unittest.TestCase):
    def test_broker_executes_only_requested_read_only_tool_in_declared_namespace(self):
        from unittest.mock import Mock

        agent = SimpleNamespace(id="sre", tools=("workload_state",), max_requests=3, max_tool_calls=1)
        incident = {"namespace": "staging", "workload": "web"}
        executor = Mock()
        executor.execute.return_value = {"ready": 1}
        replies = iter([{"status": "tool_request", "tool": "workload_state"},
                        {"status": "final", "answer": "Observed ready replica"}])
        payloads = []

        def invoke(endpoint, name, payload, credential):
            payloads.append(payload)
            return next(replies)

        result = run_broker(agent, incident, "https://example.test/project", "staging",
                            credential=object(), executor=executor, invoker=invoke)
        self.assertEqual(result["tool_calls"], ["workload_state"])
        self.assertEqual(payloads[1]["history"], [{"name": "workload_state", "result": {"ready": 1}}])
        executor.execute.assert_called_once_with("workload_state", incident, "staging")

        with self.assertRaisesRegex(ValueError, "approved namespace"):
            run_broker(agent, incident, "https://example.test/project", "production",
                       credential=object(), executor=executor, invoker=invoke)

    def test_broker_reuses_a_pinned_session_for_every_tool_turn(self):
        from unittest.mock import Mock

        agent = SimpleNamespace(id="sre", tools=("workload_state",), max_requests=3, max_tool_calls=1)
        incident = {"namespace": "staging", "workload": "web"}
        replies = iter([{"status": "tool_request", "tool": "workload_state"},
                        {"status": "final", "answer": "One ready replica"}])
        invoker = Mock(side_effect=lambda *args, **kwargs: next(replies))
        executor = Mock()
        executor.execute.return_value = {"ready": 1}
        run_broker(agent, incident, "https://example.test/project", "staging", version="2",
                   session_id="pinned-session", credential=object(), executor=executor, invoker=invoker)
        self.assertEqual([call.kwargs["session_id"] for call in invoker.call_args_list],
                         ["pinned-session", "pinned-session"])

    def test_broker_rejects_unknown_tool_before_kubernetes_call(self):
        from unittest.mock import Mock

        agent = SimpleNamespace(id="sre", tools=("workload_state",), max_requests=2, max_tool_calls=1)
        executor = Mock()
        with self.assertRaisesRegex(ValueError, "unknown or repeated"):
            run_broker(agent, {"namespace": "staging", "workload": "web"}, "https://example.test", "staging",
                       credential=object(), executor=executor,
                       invoker=lambda *_: {"status": "tool_request", "tool": "delete_pod"})
        executor.execute.assert_not_called()

    def test_diagnostic_adapter_uses_fixed_read_only_kubernetes_paths(self):
        from unittest.mock import Mock

        api = Mock()
        deployment = {"metadata": {"generation": 2}, "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "web"}}},
                      "status": {"observedGeneration": 2, "readyReplicas": 1}}
        pods = {"items": [{"metadata": {"name": "web-1"}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 0}]}}]}
        api.get.side_effect = [SimpleNamespace(json=lambda: deployment, raise_for_status=lambda: None),
                               SimpleNamespace(json=lambda: pods, raise_for_status=lambda: None)]
        result = KubernetesDiagnostics(api_client=api, metrics_client=Mock()).execute(
            "workload_state", {"namespace": "staging", "workload": "web"}, "staging")
        self.assertEqual(result["deployment"]["ready"], 1)
        self.assertEqual(api.get.call_args_list[0].args[0], "/apis/apps/v1/namespaces/staging/deployments/web")
        self.assertEqual(api.get.call_args_list[1].kwargs["params"]["labelSelector"], "app=web")
        with self.assertRaisesRegex(ValueError, "read-only registry"):
            KubernetesDiagnostics(api_client=api, metrics_client=Mock()).execute("delete_pod", {}, "staging")

    def test_common_credentials_are_masked_before_log_evidence(self):
        value = 'Authorization: Bearer abc.def password="secret" api_key=hidden'
        redacted = redact(value)
        for secret in ("abc.def", "secret", "hidden"):
            self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_two_consumers_share_defaults_and_digest_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            first = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION)
            second = version_request(load_agent(agent_file(directory, "release")), IMAGE, REVISION)

        self.assertEqual(first["definition"]["protocol_versions"], [{"protocol": "invocations", "version": "2.0.0"}])
        environment = first["definition"]["environment_variables"]
        self.assertEqual(environment["PORTFOLIO_MANIFEST_PATH"], "agents/capacity.yaml")
        self.assertFalse(any(key.startswith(("AGENT_", "FOUNDRY_")) for key in environment))
        self.assertEqual(second["agent_name"], "release")
        self.assertTrue(first["draft"])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "sha256 digest"):
                version_request(load_agent(agent_file(directory, "invalid")), "registry.example.test/agents/catalog:latest", REVISION)

    @patch("azure.ai.projects.AIProjectClient")
    def test_reapply_reuses_matching_draft(self, client_class):
        with tempfile.TemporaryDirectory() as directory:
            request = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION)
        existing = SimpleNamespace(metadata=request["metadata"], version="draft-1")
        client_class.return_value.agents.list_versions.return_value = [existing]
        result = create_draft("https://foundry.example.services.ai.azure.com/api/projects/management", request, credential=object())
        self.assertIs(result, existing)
        client_class.return_value.agents.create_version.assert_not_called()

    @patch("azure.ai.projects.AIProjectClient")
    def test_new_draft_uses_official_hosted_definition(self, client_class):
        with tempfile.TemporaryDirectory() as directory:
            request = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION)
        client_class.return_value.agents.list_versions.return_value = []
        create_draft("https://foundry.example.services.ai.azure.com/api/projects/management", request, credential=object())
        call = client_class.return_value.agents.create_version.call_args
        self.assertEqual(call.kwargs["definition"].as_dict(), request["definition"])
        self.assertTrue(call.kwargs["draft"])

    @patch("azure.ai.projects.AIProjectClient")
    def test_first_release_reuses_same_revision_and_refuses_other_release(self, client_class):
        with tempfile.TemporaryDirectory() as directory:
            request = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION, draft=False)
        existing = SimpleNamespace(metadata=request["metadata"], version="1")
        client_class.return_value.agents.list_versions.return_value = [existing]
        result = create_first_release("https://foundry.example.services.ai.azure.com/api/projects/management", request, credential=object())
        self.assertIs(result, existing)
        client_class.return_value.agents.create_version.assert_not_called()

        existing.metadata = {"source_revision": "c" * 40, "image_digest": IMAGE}
        with self.assertRaisesRegex(ValueError, "promotion workflow"):
            create_first_release("https://foundry.example.services.ai.azure.com/api/projects/management", request, credential=object())

    @patch("azure.ai.projects.AIProjectClient")
    def test_new_first_release_uses_official_definition(self, client_class):
        with tempfile.TemporaryDirectory() as directory:
            request = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION, draft=False)
        client_class.return_value.agents.list_versions.return_value = []
        create_first_release("https://foundry.example.services.ai.azure.com/api/projects/management", request, credential=object())
        call = client_class.return_value.agents.create_version.call_args
        self.assertEqual(call.kwargs["definition"].as_dict(), request["definition"])
        self.assertFalse(call.kwargs["draft"])

    def test_active_status_and_structured_invocation(self):
        from unittest.mock import Mock

        active = SimpleNamespace(status="active", version="1", error=None)
        project = Mock()
        project.agents.get_version.return_value = active
        self.assertIs(wait_active(project, "capacity", "1", timeout_seconds=1), active)

        sender = Mock()
        sender.post.return_value.json.return_value = {"answer": "Need evidence"}
        credential = Mock()
        credential.get_token.return_value.token = "token"
        result = smoke("https://foundry.example.services.ai.azure.com/api/projects/management", "capacity", {"incident": {"title": "test"}}, credential, client=sender)
        self.assertEqual(result["answer"], "Need evidence")
        sender.post.return_value.raise_for_status.assert_called_once()
        smoke("https://foundry.example.services.ai.azure.com/api/projects/management", "capacity", {},
              credential, client=sender, session_id="candidate-session")
        self.assertEqual(sender.post.call_args.kwargs["params"]["agent_session_id"], "candidate-session")

    def test_candidate_requires_fixed_route_and_reuses_exact_revision(self):
        from unittest.mock import Mock

        sender = Mock()
        sender.get.return_value.json.return_value = {
            "agent_endpoint": {
                "version_selector": {"version_selection_rules": [
                    {"agent_version": "1", "traffic_percentage": 100, "type": "FixedRatio"}]},
                "protocol_configuration": {"invocations": {}},
            },
        }
        credential = Mock()
        credential.get_token.return_value.token = "token"
        require_pinned_route("https://foundry.example.test/api/projects/management", "sre", "1", credential, client=sender)
        sender.get.return_value.json.return_value["agent_endpoint"]["version_selector"]["version_selection_rules"][0]["agent_version"] = "@latest"
        with self.assertRaisesRegex(ValueError, "not fixed"):
            require_pinned_route("https://foundry.example.test/api/projects/management", "sre", "1", credential, client=sender)

        with tempfile.TemporaryDirectory() as directory:
            request = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION, draft=False)
        existing = SimpleNamespace(metadata=request["metadata"], version="2")
        with patch("azure.ai.projects.AIProjectClient") as project:
            project.return_value.agents.list_versions.return_value = [existing]
            self.assertIs(create_candidate_release("https://foundry.example.test/api/projects/management", request, credential), existing)
            project.return_value.agents.create_version.assert_not_called()

    @patch("portfolio_agent_factory.release.smoke")
    @patch("portfolio_agent_factory.release.create_pinned_session", return_value="candidate-session")
    @patch("portfolio_agent_factory.release.wait_active")
    @patch("portfolio_agent_factory.release.create_candidate_release")
    @patch("portfolio_agent_factory.release.require_pinned_route")
    @patch("portfolio_agent_factory.release.AIProjectClient")
    def test_candidate_smoke_checks_route_and_invokes_only_pinned_version(self, project, route, create, active, session, invoke):
        with tempfile.TemporaryDirectory() as directory:
            request = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION, draft=False)
        create.return_value = SimpleNamespace(version="2")
        active.return_value = SimpleNamespace(version="2", status="active")
        invoke.return_value = {"status": "final", "answer": "Need evidence"}
        result = candidate_and_smoke("https://foundry.example.test/api/projects/management", request,
                                     {"incident": {"summary": "test"}}, "1", credential=object())
        self.assertEqual(route.call_count, 2)
        self.assertEqual(result["candidate_version"], "2")
        self.assertEqual(invoke.call_args.kwargs["session_id"], "candidate-session")

    @patch("portfolio_agent_factory.model.OpenAI")
    @patch("portfolio_agent_factory.model.get_bearer_token_provider")
    @patch("portfolio_agent_factory.model.DefaultAzureCredential")
    def test_injected_model_route_requires_https(self, credential, provider, openai):
        with patch.dict("os.environ", {"MODEL_BASE_URL": "https://gateway.example.test/openai/v1/", "MODEL_TOKEN_SCOPE": "api://gateway/.default"}):
            model_client("https://foundry.example.test/api/projects/management")
        self.assertEqual(openai.call_args.kwargs["base_url"], "https://gateway.example.test/openai/v1/")
        self.assertEqual(provider.call_args.args[1], "api://gateway/.default")
        with patch.dict("os.environ", {"MODEL_BASE_URL": "http://gateway.example.test/openai/v1/"}):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                model_client("https://foundry.example.test/api/projects/management")


if __name__ == "__main__":
    unittest.main()
