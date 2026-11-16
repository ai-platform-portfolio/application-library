"""Inference connection selection and failure behavior without cloud calls."""
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import AuthenticationError, OpenAI

from portfolio_agent_runtime.agent import model_client, model_settings


class ConnectionTests(unittest.TestCase):
    def test_secret_refresh_expiry_failure_and_explicit_timeout(self):
        env = {**self.gateway(), 'MODEL_API_KEY': '',
               'MODEL_API_KEY_SECRET_URI': 'https://test.vault.azure.net/secrets/documents'}
        now = datetime.now(timezone.utc)
        def secret(value, expiry=now + timedelta(hours=1)):
            return SimpleNamespace(value=value, properties=SimpleNamespace(enabled=True, expires_on=expiry))
        with patch.dict(os.environ, env, clear=True), patch('azure.keyvault.secrets.SecretClient') as factory, patch(
                'portfolio_agent_runtime.agent.OpenAI') as openai:
            factory.return_value.__enter__.return_value.get_secret.side_effect = [secret('key-one'), secret('key-two'), RuntimeError('unavailable')]
            model_client(Mock(), timeout=45)
            self.assertEqual(openai.call_args.kwargs['timeout'], 45)
            key = openai.call_args.kwargs['api_key']
            self.assertEqual(key(), 'key-one')
            self.assertEqual(key(), 'key-one')
            key.refresh_at = 0
            self.assertEqual(key(), 'key-two')
            key.refresh_at = 0
            with self.assertRaises(RuntimeError):
                key()
            factory.return_value.__enter__.return_value.get_secret.side_effect = None
            factory.return_value.__enter__.return_value.get_secret.return_value = secret('expired', now - timedelta(seconds=1))
            with self.assertRaises(ValueError):
                key()

    def test_secret_reference_is_exclusive_and_versionless(self):
        env = {**self.gateway(), 'MODEL_API_KEY_SECRET_URI': 'https://test.vault.azure.net/secrets/test'}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
            model_settings()
        for uri in ['https://evil.test/secrets/test', 'https://test.vault.azure.net/secrets/test/version',
                    'https://test.vault.azure.net:443/secrets/test', 'https://test.vault.azure.net/secrets/a%2Fb']:
            with patch.dict(os.environ, {**env, 'MODEL_API_KEY': '', 'MODEL_API_KEY_SECRET_URI': uri}, clear=True), self.assertRaises(ValueError):
                model_settings()

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


class CapabilityTests(unittest.TestCase):
    def test_capability_cannot_borrow_the_default_key(self):
        env = {'MODEL_CONNECTION_MODE': 'gateway', 'MODEL_AUTH_METHOD': 'api_key',
               'MODEL_BASE_URL': 'https://gateway.test/v1', 'MODEL_DEPLOYMENT_NAME': 'reasoning',
               'MODEL_API_KEY': 'default-key', 'CODING_MODEL_DEPLOYMENT_NAME': 'coding'}
        with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
            model_settings(capability='coding')
        env['CODING_MODEL_API_KEY_SECRET_URI'] = 'https://test.vault.azure.net/secrets/coding'
        with patch.dict(os.environ, env, clear=True):
            settings = model_settings(capability='coding')
            self.assertEqual(settings['MODEL_DEPLOYMENT_NAME'], 'coding')
            self.assertEqual(settings['MODEL_API_KEY'], '')

    def test_embedding_dimension_and_input_order_are_verified(self):
        from portfolio_agent_runtime.agent import embeddings
        from types import SimpleNamespace
        env = {'MODEL_CONNECTION_MODE': 'gateway', 'MODEL_AUTH_METHOD': 'api_key',
               'MODEL_BASE_URL': 'https://gateway.test/v1', 'EMBEDDING_MODEL_DEPLOYMENT_NAME': 'embedding',
               'EMBEDDING_MODEL_API_KEY': 'embedding-key'}
        client = Mock()
        client.embeddings.create.return_value = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0]*256)])
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(len(embeddings(client, ['reviewed note'])[0]), 256)
            client.embeddings.create.assert_called_once_with(model='embedding', input=['reviewed note'], dimensions=256)
            client.embeddings.create.return_value.data[0].embedding = [float('nan')]*256
            with self.assertRaises(ValueError):
                embeddings(client, ['reviewed note'])
            client.embeddings.create.return_value.data[0].index = 1
            with self.assertRaises(ValueError):
                embeddings(client, ['reviewed note'])
