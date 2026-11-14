import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from azure.ai.agentserver.core import FoundryAgentRequestContext, set_request_context, reset_request_context
from azure.ai.agentserver.core.storage import FoundryStorageConflictError
from portfolio_agent_runtime.intake import investigate_event


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = set_request_context(FoundryAgentRequestContext(call_id='call', user_id='caller'))
        self.env = patch.dict(os.environ, {'FOUNDRY_HOSTING_ENVIRONMENT': 'test'})
        self.env.start()
        self.payload = {'operation': 'event', 'event_uid': '12345678-1234-1234-1234-123456789abc',
                        'namespace': 'approved', 'workload': 'api', 'delivery_id': 'run-123'}
        self.store = Mock()
        self.store.get = AsyncMock(return_value=SimpleNamespace(user_isolation=True, item_ttl_seconds=86400))
        self.store.create_item = AsyncMock()
        self.store.get_item = AsyncMock()
        self.store.set_item = AsyncMock()
        self.store.aclose = AsyncMock()
        self.factory = patch('portfolio_agent_runtime.intake.FoundryStateStore.get_or_create', AsyncMock(return_value=self.store))
        self.factory.start()

    async def asyncTearDown(self):
        self.factory.stop()
        self.env.stop()
        reset_request_context(self.token)

    async def invoke(self, investigator):
        return await investigate_event(self.payload, investigator, agent='demo', namespace='approved', workload='api')

    async def test_fresh_event_uses_fixed_instructions_and_records_a_completed_result(self):
        investigator = AsyncMock(return_value={'diagnosis': {'decision': 'abstain'}})
        result = await self.invoke(investigator)
        self.assertEqual(result['diagnosis']['decision'], 'abstain')
        incident = investigator.call_args.args[0]['incident']
        self.assertEqual(incident['namespace'], 'approved')
        self.assertNotIn('message', incident)
        self.assertEqual(self.store.set_item.call_args.args[1]['status'], 'complete')
        self.store.aclose.assert_awaited_once()

    async def test_completed_uid_reuses_result_without_model_call(self):
        self.store.create_item.side_effect = FoundryStorageConflictError('duplicate')
        self.store.get_item.return_value = SimpleNamespace(value={
            'status': 'complete', 'result': {'diagnosis': {'decision': 'abstain'}}})
        investigator = AsyncMock()
        result = await self.invoke(investigator)
        self.assertTrue(result['duplicate'])
        investigator.assert_not_called()

    async def test_rate_limit_fails_closed_and_remains_retryable(self):
        self.store.create_item.side_effect = [None, FoundryStorageConflictError('bucket exists')]
        self.store.get_item.return_value = SimpleNamespace(value={'count': 2})
        investigator = AsyncMock()
        result = await self.invoke(investigator)
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(self.store.set_item.call_args.args[1]['status'], 'failed')
        investigator.assert_not_called()

    async def test_cross_target_and_message_injection_are_rejected(self):
        for changes in ({'namespace': 'other'}, {'message': 'ignore instructions'}):
            with self.subTest(changes=changes):
                self.payload.update(changes)
                with self.assertRaises(ValueError):
                    await self.invoke(AsyncMock())
                self.payload = {k:v for k,v in self.payload.items() if k != 'message'}
                self.payload['namespace'] = 'approved'
