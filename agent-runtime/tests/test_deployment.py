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
    def test_predeploy_preserves_backend_specific_configuration(self):
        connection = {'endpoint': 'https://telemetry.test/query/consumer',
                      'scope': 'api://telemetry/.default',
                      'queries': {'events': 'query=approved&since=5m&limit=20'}}
        target = {'KUBE_ALLOWED_NAMESPACE': 'preview', 'KUBE_ALLOWED_WORKLOAD': 'api'}
        kubernetes = {'KUBE_API_URL': 'https://kubernetes.test', 'KUBE_CA_PEM': 'ca', **target}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.yaml'
            config.write_text('model: reasoning\n')
            for backend in ('provided', 'kubernetes', 'telemetry'):
                with self.subTest(backend=backend):
                    env = {'AZURE_ENV_NAME': 'repo-agent', 'AGENT_DIAGNOSTICS_BACKEND': backend,
                           'TELEMETRY_CONNECTION': json.dumps(connection), **kubernetes}
                    captured = {}

                    def capture(args, **kwargs):
                        path = Path(args[args.index('--file') + 1])
                        captured.update({key: json.loads(value) for key, value in
                                         (line.split('=', 1) for line in path.read_text().splitlines())})

                    with patch.dict(os.environ, env, clear=True), \
                         patch('portfolio_agent_runtime.deployment.model_settings', return_value={}), \
                         patch('portfolio_agent_runtime.deployment.ssl.create_default_context') as tls, \
                         patch('portfolio_agent_runtime.deployment.subprocess.run', side_effect=capture):
                        configure(config)
                    self.assertEqual(captured['AGENT_DIAGNOSTICS_BACKEND'], backend)
                    for key, value in kubernetes.items():
                        expected = value if backend == 'kubernetes' or (backend == 'telemetry' and key in target) else ''
                        self.assertEqual(captured[key], expected)
                    if backend == 'kubernetes':
                        tls.assert_called_once_with(cadata='ca')
                    else:
                        tls.assert_not_called()
                    if backend == 'telemetry':
                        self.assertEqual(json.loads(captured['TELEMETRY_CONNECTION']), connection)
                    else:
                        self.assertEqual(captured['TELEMETRY_CONNECTION'], '')

    def test_predeploy_rejects_non_https_telemetry_before_azd(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.yaml'
            config.write_text('model: reasoning\n')
            env = {'AZURE_ENV_NAME': 'repo-agent', 'AGENT_DIAGNOSTICS_BACKEND': 'telemetry',
                   'KUBE_ALLOWED_NAMESPACE': 'preview', 'KUBE_ALLOWED_WORKLOAD': 'api',
                   'TELEMETRY_CONNECTION': json.dumps({'endpoint': 'http://telemetry.test/query',
                                                        'scope': 'api://telemetry/.default',
                                                        'queries': {'events': 'query=approved'}})}
            with patch.dict(os.environ, env, clear=True), \
                 patch('portfolio_agent_runtime.deployment.model_settings', return_value={}), \
                 patch('portfolio_agent_runtime.deployment.subprocess.run') as azd, \
                 self.assertRaisesRegex(ValueError, 'Telemetry endpoint must use validated HTTPS'):
                configure(config)
            azd.assert_not_called()

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
