import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portfolio_agent_factory.manifest import load_agent
from portfolio_agent_factory.publish import create_draft, version_request
from portfolio_agent_factory.model import model_client


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
    def test_two_consumers_share_defaults_and_digest_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            first = version_request(load_agent(agent_file(directory, "capacity")), IMAGE, REVISION)
            second = version_request(load_agent(agent_file(directory, "release")), IMAGE, REVISION)

        self.assertEqual(first["definition"]["protocol_versions"], [{"protocol": "invocations", "version": "2.0.0"}])
        self.assertEqual(first["definition"]["environment_variables"]["AGENT_MANIFEST"], "agents/capacity.yaml")
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
