"""Native hook forwards declared optional connections without changing resources."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from portfolio_agent_runtime.deployment import configure


class DeploymentTests(unittest.TestCase):
    def capture(self, arguments, **kwargs):
        path = Path(arguments[arguments.index('--file') + 1])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.values = dict(line.split('=', 1) for line in path.read_text().splitlines())
        self.values = {key: json.loads(value) for key, value in self.values.items()}
        self.variables_path = path

    def forwarded(self):
        self.assertFalse(self.variables_path.exists())
        return [f'{key}={value}' for key, value in self.values.items()]

    def test_optional_connections_and_safe_defaults_reach_native_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.yaml'
            config.write_text('model: chat\nmemory:\n  enabled: false\n')
            with patch.dict(os.environ, {
                'AZURE_ENV_NAME': 'catalog-example', 'AGENT_DIAGNOSTICS_BACKEND': 'provided',
                'MODEL_BASE_URL': 'https://model.example/v1', 'MODEL_TOKEN_SCOPE': 'api://model/.default',
                'METRICS_URL': 'https://metrics.example', 'METRICS_TOKEN_SCOPE': 'api://metrics/.default',
            }), patch('portfolio_agent_runtime.deployment.AIProjectClient') as project, patch(
                    'portfolio_agent_runtime.deployment.subprocess.run', side_effect=self.capture) as command:
                configure(config)
            project.assert_not_called()
            arguments = self.forwarded()
            for value in ('MODEL_BASE_URL=https://model.example/v1', 'MODEL_TOKEN_SCOPE=api://model/.default',
                          'METRICS_URL=https://metrics.example', 'METRICS_TOKEN_SCOPE=api://metrics/.default',
                          'MEMORY_STORE_NAME=', 'KUBE_API_URL='):
                self.assertIn(value, arguments)

    def test_empty_optional_connections_preserve_the_default_model_route(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.yaml'
            config.write_text('model: chat\nmemory:\n  enabled: false\n')
            with patch.dict(os.environ, {
                'AZURE_ENV_NAME': 'catalog-example',
                'MODEL_BASE_URL': '', 'MODEL_TOKEN_SCOPE': '',
                'METRICS_URL': '', 'METRICS_TOKEN_SCOPE': '',
            }, clear=True), patch('portfolio_agent_runtime.deployment.AIProjectClient') as project, patch(
                    'portfolio_agent_runtime.deployment.subprocess.run', side_effect=self.capture) as command:
                configure(config)
            project.assert_not_called()
            arguments = self.forwarded()
            for value in ('MODEL_BASE_URL=', 'MODEL_TOKEN_SCOPE=https://ai.azure.com/.default',
                          'METRICS_URL=', 'METRICS_TOKEN_SCOPE=', 'AGENT_DIAGNOSTICS_BACKEND=provided'):
                self.assertIn(value, arguments)

    def test_gateway_credentials_use_owner_only_native_file_and_validate_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.yaml'
            config.write_text('model: route\nmemory:\n  enabled: false\n')
            env = {'AZURE_ENV_NAME': 'example', 'MODEL_CONNECTION_MODE': 'gateway',
                   'MODEL_AUTH_METHOD': 'api_key', 'MODEL_BASE_URL': 'https://gateway.test/v1',
                   'MODEL_API_KEY': 'test-secret'}
            with patch.dict(os.environ, env, clear=True), patch(
                    'portfolio_agent_runtime.deployment.subprocess.run', side_effect=self.capture) as command:
                configure(config)
                self.assertEqual(self.values['MODEL_DEPLOYMENT_NAME'], 'route')
                self.assertEqual(self.values['MODEL_API_KEY'], 'test-secret')
                self.assertNotIn('test-secret', str(command.call_args))
                self.forwarded()

                def fail(arguments, **kwargs):
                    self.capture(arguments, **kwargs)
                    raise subprocess.CalledProcessError(1, arguments)

                command.side_effect = fail
                with self.assertRaises(subprocess.CalledProcessError) as error:
                    configure(config)
                self.assertFalse(self.variables_path.exists())
                self.assertNotIn('test-secret', str(error.exception))
            del env['MODEL_BASE_URL']
            with patch.dict(os.environ, env, clear=True), patch(
                    'portfolio_agent_runtime.deployment.AIProjectClient') as project, patch(
                    'portfolio_agent_runtime.deployment.subprocess.run') as command:
                with self.assertRaises(ValueError):
                    configure(config)
                project.assert_not_called()
                command.assert_not_called()

    def test_memory_processing_model_is_independent_of_inference_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'knowledge').mkdir()
            config = root / 'agent.yaml'
            config.write_text('model: investigator\nmemory:\n  enabled: true\n  chat_model: memory-chat\nknowledge:\n  path: knowledge\n')
            env = {'AZURE_ENV_NAME': 'example', 'MODEL_DEPLOYMENT_NAME': 'gateway-route',
                   'FOUNDRY_PROJECT_ENDPOINT': 'https://foundry.test/project',
                   'GITHUB_WORKSPACE': directory, 'GITHUB_REPOSITORY': 'org/repo',
                   'SOURCE_REVISION': 'a' * 40, 'RUNNER_TEMP': directory}
            previous = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(os.environ, env, clear=True), patch(
                        'portfolio_agent_runtime.deployment.DefaultAzureCredential'), patch(
                        'portfolio_agent_runtime.deployment.AIProjectClient') as project, patch(
                        'portfolio_agent_runtime.deployment.ensure_memory') as memory, patch(
                        'portfolio_agent_runtime.deployment.publish', return_value={}), patch(
                        'portfolio_agent_runtime.deployment.subprocess.run', side_effect=self.capture):
                    configure(config)
                self.assertEqual(memory.call_args.args[2:4], ('memory-chat', 'embedding'))
                self.assertEqual(project.call_args.kwargs['endpoint'], env['FOUNDRY_PROJECT_ENDPOINT'])
                self.assertEqual(self.values['MODEL_DEPLOYMENT_NAME'], 'gateway-route')
            finally:
                os.chdir(previous)
