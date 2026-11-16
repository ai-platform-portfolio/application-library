"""Behavioral checks for approved context, concurrent capacity and gateway retrieval."""
import asyncio
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from azure.ai.agentserver.core import FoundryAgentRequestContext, set_request_context, reset_request_context
from azure.ai.agentserver.core.storage import FoundryStorageConflictError, FoundryStoragePreconditionError
from portfolio_agent_runtime.context import Context, user_context
from portfolio_agent_runtime.knowledge import corpus, publish, knowledge_query
from portfolio_agent_runtime.hosting import invoke

VECTOR = [1.0] + [0.0] * 255
ENV = {'EMBEDDING_MODEL_DEPLOYMENT_NAME': 'embedding', 'EMBEDDING_MODEL_VERSION': '1'}
REVIEW = {'approved': True, 'url': 'https://example.test/review'}


class State:
    """Atomic native storage contract; yield reads to exercise cross-request races."""
    def __init__(self):
        self.items = {}
        self.generation = 0

    async def get_item(self, key):
        value = self.items.get(key)
        result = SimpleNamespace(value=json.loads(json.dumps(value[0])), etag=value[1]) if value else None
        await asyncio.sleep(0)
        return result

    async def create_item(self, key, value):
        if key in self.items:
            raise FoundryStorageConflictError('conflict')
        await self.set_item(key, value)

    async def set_item(self, key, value, if_match=None):
        if if_match is not None and (key not in self.items or self.items[key][1] != if_match):
            raise FoundryStoragePreconditionError('changed')
        self.generation += 1
        self.items[key] = (json.loads(json.dumps(value)), str(self.generation))

    async def delete_item(self, key):
        self.items.pop(key, None)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_identity_isolation_and_retention_remain_mandatory(self):
        state = Mock(get=AsyncMock(return_value=SimpleNamespace(user_isolation=True, item_ttl_seconds=2592000)), aclose=AsyncMock())
        with patch('portfolio_agent_runtime.context.FoundryStateStore.get_or_create', AsyncMock(return_value=state)) as factory:
            with self.assertRaises(ValueError):
                async with user_context('demo', Mock(), ''):
                    pass
            scopes = []
            for agent, user in [('demo', 'one'), ('demo', 'two'), ('other', 'one')]:
                token = set_request_context(FoundryAgentRequestContext(call_id='trusted-call', user_id=user))
                try:
                    with patch.dict(os.environ, {'FOUNDRY_HOSTING_ENVIRONMENT': 'test'}):
                        async with user_context(agent, Mock(), '') as context:
                            scopes.append(context.scope)
                finally:
                    reset_request_context(token)
            self.assertEqual(len(set(scopes)), 3)
            self.assertEqual(scopes[0], 'demo-' + hashlib.sha256(b'demo\0one').hexdigest())
            self.assertTrue(all(c.kwargs['user_isolation'] for c in factory.call_args_list))
        self.assertEqual(state.aclose.await_count, 3)

    async def test_review_recall_restart_expiry_and_deletion(self):
        state = State()
        context = Context(state, Mock(), '', 'caller', Mock())
        with patch.dict(os.environ, ENV), patch('portfolio_agent_runtime.context.embeddings', side_effect=lambda c, t: [VECTOR for _ in t]):
            with self.assertRaises(ValueError):
                await context.remember('An unapproved operational note.', {**REVIEW, 'approved': False})
            saved = await context.remember('An explicitly approved operational note.', REVIEW)
            # A fresh context instance reads the persisted note, not process memory.
            other_session = Context(state, Mock(), '', 'caller', Mock())
            self.assertEqual((await other_session.recall('operational'))[0]['id'], saved['id'])
            await other_session.forget(saved['id'])
            self.assertEqual(await context.recall('operational'), [])
            saved = await context.remember('A second explicitly approved operational note.', REVIEW)
            with patch('portfolio_agent_runtime.context.time.time', return_value=time.time() + 2592001):
                self.assertEqual(await context.recall('operational'), [])
            await context.checkpoint('case', {'trace_id': 'trace'})
            self.assertEqual(await other_session.resume('case'), {'trace_id': 'trace'})
            await context.forget_investigation('case')
            self.assertIsNone(await other_session.resume('case'))

    async def test_two_writers_cannot_exceed_capacity_or_lose_a_note(self):
        context = Context(State(), Mock(), '', 'caller', Mock())
        with patch.dict(os.environ, ENV), patch('portfolio_agent_runtime.context.embeddings', side_effect=lambda c, t: [VECTOR for _ in t]):
            for i in range(99):
                await context.remember(f'Approved context note number {i}.', REVIEW)
            results = await asyncio.gather(context.remember('Approved concurrent note one.', REVIEW),
                                           context.remember('Approved concurrent note two.', REVIEW), return_exceptions=True)
            self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
            stored = await context.state.get_item('approved-notes-v1')
            self.assertEqual(len(stored.value['notes']), 100)
            self.assertEqual(len({n['id'] for n in stored.value['notes']}), 100)
            with patch.dict(os.environ, {'EMBEDDING_MODEL_VERSION': '2'}), self.assertRaisesRegex(ValueError, 'reindex'):
                await context.recall('operational')

    async def test_legacy_uses_only_scoped_reads_and_preserves_ids_expiry_and_forget(self):
        memories = Mock()
        note = SimpleNamespace(memory_id='legacy-' + 'a'*143, scope='caller', updated_at=datetime.now(timezone.utc),
                               content=json.dumps({'content': 'Previously approved operational note.', 'review': REVIEW['url']}))
        memories.list_memories.return_value = [note]
        context = Context(State(), memories, 'legacy-store', 'caller', Mock())
        with patch.dict(os.environ, {**ENV, 'LEGACY_MEMORY_UNTIL': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}), \
             patch('portfolio_agent_runtime.context.embeddings', side_effect=lambda c, t: [VECTOR for _ in t]):
            recalled = await context.recall('operational')
            self.assertEqual(recalled[0]['id'], note.memory_id)
            self.assertEqual(context.state.items, {})  # no legacy plaintext migration
            await context.forget(note.memory_id)
            memories.delete_memory.assert_called_once_with('legacy-store', note.memory_id, headers={})
            note.scope = 'another-caller'
            with self.assertRaisesRegex(ValueError, 'scope'):
                await context.recall('operational')
        memories.search_memories.assert_not_called()
        memories.create_memory.assert_not_called()
        with patch.dict(os.environ, {'LEGACY_MEMORY_UNTIL': ''}), self.assertRaisesRegex(ValueError, 'deadline'):
            await context.legacy()

    async def test_grader_and_oversized_checkpoint_rejected(self):
        state = State()
        context = Context(state, Mock(), '', 'caller')
        for value in ({'nested': {'rubric': {}}}, {'answer': 'x'*8192}, {'evidence': {}}):
            with self.assertRaises(ValueError):
                await context.checkpoint('case', value)
        self.assertFalse(state.items)

    async def test_replays_never_open_persistent_context(self):
        with patch('portfolio_agent_runtime.hosting.user_context') as factory:
            with self.assertRaises(ValueError):
                await invoke({'incident': {'summary': 'replay'}, 'evidence': {}}, Mock(), agent='demo', project=Mock(), memory_store='')
            factory.assert_not_called()

    async def test_runbooks_are_revision_pinned_grounded_cached_and_bounded(self):
        state = State()
        state.get = AsyncMock(return_value=SimpleNamespace(user_isolation=False, item_ttl_seconds=2592000))
        state.aclose = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ENV), \
             patch('portfolio_agent_runtime.knowledge.FoundryStateStore.get_or_create', AsyncMock(return_value=state)), \
             patch('portfolio_agent_runtime.knowledge.embeddings', side_effect=lambda c, t: [VECTOR for _ in t]) as embed:
            path = Path(directory, 'runbook.md')
            path.write_text('Reviewed operational knowledge; validate before proposing.')
            manifest = publish(directory, 'a'*40, 'org/repo', 'agents/demo/knowledge')
            result = await knowledge_query('demo', Mock(), manifest, directory, 'validate')
            self.assertEqual(result['citations'], manifest['sources'])
            self.assertIn('validate before', result['result']['data'][0]['content'][0]['text'])
            self.assertEqual(embed.call_count, 2)
            await knowledge_query('demo', Mock(), manifest, directory, 'validate again')
            self.assertEqual(embed.call_count, 3)  # immutable runbook vector reused
            path.write_text('Changed without a matching reviewed release.')
            with self.assertRaisesRegex(ValueError, 'publication'):
                await knowledge_query('demo', Mock(), manifest, directory, 'validate')
            path.unlink()
            (Path(directory) / 'linked.md').symlink_to('/etc/hosts')
            with self.assertRaises(ValueError):
                corpus(directory)
