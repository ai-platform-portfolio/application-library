"""Native hook forwards declared optional connections without changing resources."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from portfolio_agent_runtime.deployment import configure


class DeploymentTests(unittest.TestCase):
    def test_optional_connections_and_safe_defaults_reach_native_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.yaml'
            config.write_text('memory:\n  enabled: false\n')
            with patch.dict(os.environ, {
                'AZURE_ENV_NAME': 'catalog-example', 'AGENT_DIAGNOSTICS_BACKEND': 'provided',
                'MODEL_BASE_URL': 'https://model.example/v1', 'MODEL_TOKEN_SCOPE': 'api://model/.default',
                'METRICS_URL': 'https://metrics.example', 'METRICS_TOKEN_SCOPE': 'api://metrics/.default',
            }), patch('portfolio_agent_runtime.deployment.AIProjectClient') as project, patch(
                    'portfolio_agent_runtime.deployment.subprocess.run') as command:
                configure(config)
            project.assert_not_called()
            arguments = command.call_args.args[0]
            for value in ('MODEL_BASE_URL=https://model.example/v1', 'MODEL_TOKEN_SCOPE=api://model/.default',
                          'METRICS_URL=https://metrics.example', 'METRICS_TOKEN_SCOPE=api://metrics/.default',
                          'MEMORY_STORE_NAME=', 'KUBE_API_URL='):
                self.assertIn(value, arguments)

    def test_empty_optional_connections_preserve_the_default_model_route(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'agent.yaml'
            config.write_text('memory:\n  enabled: false\n')
            with patch.dict(os.environ, {
                'AZURE_ENV_NAME': 'catalog-example',
                'MODEL_BASE_URL': '', 'MODEL_TOKEN_SCOPE': '',
                'METRICS_URL': '', 'METRICS_TOKEN_SCOPE': '',
            }, clear=True), patch('portfolio_agent_runtime.deployment.AIProjectClient') as project, patch(
                    'portfolio_agent_runtime.deployment.subprocess.run') as command:
                configure(config)
            project.assert_not_called()
            arguments = command.call_args.args[0]
            for value in ('MODEL_BASE_URL=', 'MODEL_TOKEN_SCOPE=https://ai.azure.com/.default',
                          'METRICS_URL=', 'METRICS_TOKEN_SCOPE=', 'AGENT_DIAGNOSTICS_BACKEND=provided'):
                self.assertIn(value, arguments)
