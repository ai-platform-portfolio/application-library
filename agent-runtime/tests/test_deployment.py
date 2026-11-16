"""Deployment only computes source provenance and forwards scoped public references."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from azure.core.exceptions import ResourceNotFoundError

from portfolio_agent_runtime.deployment import configure


class DeploymentTests(unittest.TestCase):
    def test_gateway_metadata_reaches_native_environment_without_model_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'knowledge').mkdir()
            (root / 'knowledge/runbook.md').write_text('Reviewed runbook')
            (root / 'agent.yaml').write_text('model: reasoning\nmemory: {enabled: true, retention_days: 30}\nknowledge: {path: knowledge}\n')
            env = {'AZURE_ENV_NAME': 'repo-agent', 'GITHUB_WORKSPACE': directory, 'GITHUB_REPOSITORY': 'org/repo',
                   'SOURCE_REVISION': 'a'*40, 'RUNNER_TEMP': directory, 'MODEL_CONNECTION_MODE': 'gateway',
                   'MODEL_AUTH_METHOD': 'api_key', 'MODEL_BASE_URL': 'https://gateway.test/v1',
                   'EMBEDDING_MODEL_VERSION': '1', 'FOUNDRY_PROJECT_ENDPOINT': 'https://foundry.test/api/projects/test'}
            for prefix, model in [('', 'reasoning'), ('CODING_', 'coding'), ('EMBEDDING_', 'embedding')]:
                env[prefix + 'MODEL_DEPLOYMENT_NAME'] = model
                env[prefix + 'MODEL_API_KEY_SECRET_URI'] = 'https://example.vault.azure.net/secrets/' + model
            captured = {}
            def capture(args, **kwargs):
                path = Path(args[args.index('--file') + 1])
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                captured.update({k: json.loads(v) for k, v in (line.split('=', 1) for line in path.read_text().splitlines())})
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, env, clear=True), patch('portfolio_agent_runtime.deployment.subprocess.run', side_effect=capture), \
                     patch('portfolio_agent_runtime.deployment.DefaultAzureCredential'), \
                     patch('portfolio_agent_runtime.deployment.AIProjectClient') as project:
                    native = project.return_value.__enter__.return_value
                    native.beta.memory_stores.get.side_effect = ResourceNotFoundError('fresh environment')
                    configure('agent.yaml')
                    native.beta.memory_stores.create.assert_not_called()
            finally:
                os.chdir(previous)
            self.assertEqual(captured['EMBEDDING_MODEL_VERSION'], '1')
            self.assertEqual(captured['CODING_MODEL_DEPLOYMENT_NAME'], 'coding')
            manifest = json.loads(captured['KNOWLEDGE_MANIFEST_JSON'])
            self.assertEqual(manifest['revision'], 'a'*40)
            self.assertNotIn('vector_store', manifest)
            self.assertIn('/blob/' + 'a'*40 + '/', manifest['sources'][0]['url'])
            self.assertFalse(captured['MODEL_API_KEY'])
            self.assertEqual(captured['MEMORY_STORE_NAME'], '')
