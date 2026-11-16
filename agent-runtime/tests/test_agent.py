"""Inference connection selection and failure behavior without cloud calls."""
import os
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import AuthenticationError, OpenAI

from portfolio_agent_runtime.agent import model_client, model_settings


class ConnectionTests(unittest.TestCase):
    def test_direct_foundry_keeps_project_derivation_and_token_refresh(self):
        credential = Mock()
        with patch.dict(os.environ, {}, clear=True), patch(
                'portfolio_agent_runtime.agent.get_bearer_token_provider', return_value=lambda: 'fresh') as provider, patch(
                'portfolio_agent_runtime.agent.OpenAI') as client:
            model_client(credential, 'https://foundry.test/api/projects/demo')
        provider.assert_called_once_with(credential, 'https://ai.azure.com/.default')
        self.assertEqual(client.call_args.kwargs['base_url'], 'https://foundry.test/api/projects/demo/openai/v1/')
        self.assertEqual(client.call_args.kwargs['api_key'](), 'fresh')
        self.assertEqual(client.call_args.kwargs['max_retries'], 0)

    def test_existing_entra_endpoint_override_still_works(self):
        env = {'MODEL_BASE_URL': 'https://custom.test/models/v1', 'MODEL_TOKEN_SCOPE': 'api://custom/.default'}
        with patch.dict(os.environ, env, clear=True), patch(
                'portfolio_agent_runtime.agent.get_bearer_token_provider') as provider, patch(
                'portfolio_agent_runtime.agent.OpenAI') as client:
            model_client(Mock())
        self.assertEqual(client.call_args.kwargs['base_url'], env['MODEL_BASE_URL'] + '/')
        self.assertEqual(provider.call_args.args[1], env['MODEL_TOKEN_SCOPE'])

    def gateway(self):
        return {'MODEL_CONNECTION_MODE': 'gateway', 'MODEL_AUTH_METHOD': 'api_key',
                'MODEL_BASE_URL': 'https://gateway.test/v1', 'MODEL_API_KEY': 'scoped-test-key',
                'MODEL_DEPLOYMENT_NAME': 'approved-route',
                'FOUNDRY_PROJECT_ENDPOINT': 'https://foundry.test/project'}

    def test_gateway_failures_never_acquire_entra_tokens_or_call_foundry(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(401, json={'error': {'message': 'Rejected', 'type': 'authentication_error'}})

        def construct(**kwargs):
            return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

        with patch.dict(os.environ, self.gateway(), clear=True), patch(
                'portfolio_agent_runtime.agent.get_bearer_token_provider') as provider, patch(
                'portfolio_agent_runtime.agent.OpenAI', side_effect=construct):
            with model_client(None) as client, self.assertRaises(AuthenticationError):
                client.responses.create(model='approved-route', input='test')
        provider.assert_not_called()
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), 'https://gateway.test/v1/responses')
        self.assertEqual(requests[0].headers['authorization'], 'Bearer scoped-test-key')

    def test_gateway_entra_requires_its_own_audience(self):
        env = {**self.gateway(), 'MODEL_AUTH_METHOD': 'entra', 'MODEL_TOKEN_SCOPE': 'api://gateway/.default'}
        with patch.dict(os.environ, env, clear=True), patch(
                'portfolio_agent_runtime.agent.get_bearer_token_provider') as provider, patch(
                'portfolio_agent_runtime.agent.OpenAI'):
            model_client(Mock())
        self.assertEqual(provider.call_args.args[1], 'api://gateway/.default')

    def test_missing_gateway_inputs_and_invalid_combinations_fail_before_client_creation(self):
        cases = [{**self.gateway(), key: value} for key, value in [
            ('MODEL_BASE_URL', ''), ('MODEL_BASE_URL', ' '), ('MODEL_API_KEY', ''),
            ('MODEL_API_KEY', 'bad\nkey'), ('MODEL_DEPLOYMENT_NAME', ''),
            ('MODEL_AUTH_METHOD', ''), ('MODEL_AUTH_METHOD', 'unknown'),
            ('MODEL_CONNECTION_MODE', 'unknown'), ('MODEL_CONNECTION_MODE', 'foundry'),
            ('MODEL_BASE_URL', 'http://gateway.test'), ('MODEL_BASE_URL', 'https://user:key@gateway.test'),
            ('MODEL_BASE_URL', 'https://gateway.test?key=secret'),
            ('MODEL_BASE_URL', 'https://gateway.test/#fragment'),
        ]]
        cases.append({**self.gateway(), 'MODEL_AUTH_METHOD': 'entra'})
        for env in cases:
            with self.subTest(env={k: v for k, v in env.items() if k != 'MODEL_API_KEY'}), patch.dict(
                    os.environ, env, clear=True), patch('portfolio_agent_runtime.agent.OpenAI') as client, patch(
                    'portfolio_agent_runtime.agent.get_bearer_token_provider') as provider:
                with self.assertRaises(ValueError) as error:
                    model_client(Mock())
                self.assertNotIn('scoped-test-key', str(error.exception))
                client.assert_not_called()
                provider.assert_not_called()

    def test_direct_mode_clears_an_unused_key_and_rejects_blank_model(self):
        with patch.dict(os.environ, {'MODEL_API_KEY': 'unused'}, clear=True):
            self.assertEqual(model_settings('chat')['MODEL_API_KEY'], '')
        with patch.dict(os.environ, {'MODEL_DEPLOYMENT_NAME': ' '}, clear=True), self.assertRaises(ValueError):
            model_settings('chat')
