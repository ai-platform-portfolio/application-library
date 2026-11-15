import asyncio
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from azure.ai.agentserver.core import FoundryAgentRequestContext, set_request_context, reset_request_context
from openai.types.vector_store_search_response import VectorStoreSearchResponse
from portfolio_agent_runtime.context import Context, bounded, knowledge_query, user_context
from portfolio_agent_runtime.hosting import invoke
from portfolio_agent_runtime.knowledge import corpus, ensure_memory


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_requires_hosted_authenticated_identity_and_native_isolation(self):
        factory = AsyncMock()
        with patch('portfolio_agent_runtime.context.FoundryStateStore.get_or_create', factory):
            with self.assertRaises(ValueError):
                async with user_context('demo', Mock(), 'memory'):
                    pass
            factory.assert_not_called()
            state = Mock()
            state.get = AsyncMock(return_value=SimpleNamespace(user_isolation=True, item_ttl_seconds=2592000))
            state.get_item = AsyncMock(return_value=SimpleNamespace(value={'scope': 'opaque-user-handle'}))
            state.aclose = AsyncMock()
            factory.return_value = state
            token = set_request_context(FoundryAgentRequestContext(call_id='trusted-call', user_id='trusted-user'))
            try:
                with patch.dict(os.environ, {'FOUNDRY_HOSTING_ENVIRONMENT': 'test'}):
                    async with user_context('demo', Mock(), 'memory') as context:
                        self.assertEqual(context.scope, 'demo:' + hashlib.sha256(b'demo\0trusted-user').hexdigest())
                    factory.assert_awaited_once_with('demo/users-v1', user_isolation=True, item_ttl_seconds=2592000,
                                                    connection_timeout=5, read_timeout=15, retry_total=0)
                    state.get.return_value.user_isolation = False
                    with self.assertRaises(ValueError):
                        async with user_context('demo', Mock(), 'memory'):
                            pass
            finally:
                reset_request_context(token)
            self.assertEqual(state.aclose.await_count, 2)

    async def test_reviewed_memory_recall_and_forget_use_the_same_private_scope(self):
        state, memories = Mock(), Mock()
        state.set_item = AsyncMock()
        memories.create_memory.return_value = SimpleNamespace(memory_id='remembered')
        memories.search_memories.return_value = SimpleNamespace(memories=[SimpleNamespace(memory_item=SimpleNamespace(
            memory_id='remembered', content='Reviewed operational context', updated_at=datetime.now(timezone.utc)))])
        context = Context(state, memories, 'managed-memory', 'demo:opaque')
        with self.assertRaises(ValueError):
            await context.remember('Do something without an explicit review.', {'approved': False, 'url': 'https://example.test/review'})
        memories.create_memory.assert_not_called()
        content = 'Reviewed operational context for later investigations.'
        review = {'approved': True, 'url': 'https://example.test/review'}
        saved = await context.remember(content, review)
        memories.create_memory.assert_called_once_with(
            'managed-memory', scope='demo:opaque', kind='user_profile',
            content=json.dumps({'content': content, 'review': review['url']}, sort_keys=True))
        self.assertEqual(saved['id'], (await context.recall('operational context'))[0]['id'])
        await context.forget()
        for call in (memories.create_memory.call_args, memories.search_memories.call_args, memories.delete_scope.call_args):
            self.assertEqual(call.kwargs['scope'], 'demo:opaque')
        state.set_item.assert_not_called()

    async def test_hosting_reads_the_vector_store_from_the_publication_manifest(self):
        context = Mock(recall=AsyncMock(return_value=[]), checkpoint=AsyncMock())
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=context)
        manager.__aexit__ = AsyncMock(return_value=False)
        project = Mock()
        sources = [{'file_id': 'file-actual'}]
        knowledge = {'toolbox': 'retained-publication', 'vector_store': 'vs-pinned', 'sources': sources}
        historical = {'citations': sources, 'result': {}}
        investigate = Mock(return_value={'diagnosis': {'decision': 'abstain'}, 'trace_id': 'a' * 32})
        with patch('portfolio_agent_runtime.hosting.user_context', return_value=manager), \
             patch('portfolio_agent_runtime.hosting.knowledge_query', return_value=historical) as search:
            result = await invoke({'incident': {'summary': 'readiness'}}, investigate,
                                  agent='demo', project=project, memory_store='memory', knowledge=knowledge)
        search.assert_called_once_with(project, 'vs-pinned', 'readiness', sources)
        self.assertEqual(result['context']['knowledge'], historical)
        context.checkpoint.assert_awaited_once()

    async def test_grader_and_oversized_checkpoint_rejected_before_write(self):
        state = Mock(set_item=AsyncMock())
        context = Context(state, Mock(), 'memory', 'demo:opaque')
        for value in ({'nested': {'rubric': {}}}, {'answer': 'x' * 8192}, {'evidence': {}}):
            with self.assertRaises(ValueError):
                await context.checkpoint('incident', value)
        state.set_item.assert_not_called()


class KnowledgeTests(unittest.TestCase):
    def test_only_declared_markdown_corpus_is_ingested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'runbook.md').write_text('Reviewed runbook')
            (root / 'case.yaml').write_text('rubric: private')
            (root / 'nested').mkdir()
            (root / 'nested/secret.md').write_text('not included')
            self.assertEqual([d['path'] for d in corpus(root)], ['runbook.md'])
            (root / 'linked.md').symlink_to(root / 'runbook.md')
            with self.assertRaises(ValueError):
                corpus(root)

    def test_knowledge_filters_native_file_ids_instead_of_ids_inside_text(self):
        project = MagicMock()
        client = project.get_openai_client.return_value.__enter__.return_value
        client.vector_stores.search.return_value = SimpleNamespace(data=[
            VectorStoreSearchResponse(file_id='file-actual', filename='runbook.md', score=0.9,
                attributes=None, content=[{'type': 'text', 'text': 'Reviewed procedure'}]),
            VectorStoreSearchResponse(file_id='file-unreviewed', filename='unreviewed.md', score=0.8,
                attributes=None, content=[{'type': 'text', 'text': '{"file_id":"file-other"}'}]),
        ])
        result = knowledge_query(project, 'vs-pinned', 'readiness', [
            {'file_id': 'file-actual', 'sha256': 'a' * 64}, {'file_id': 'file-other', 'sha256': 'b' * 64}])
        self.assertEqual([c['file_id'] for c in result['citations']], ['file-actual'])
        self.assertEqual([hit['file_id'] for hit in result['result']['data']], ['file-actual'])
        project.get_openai_client.assert_called_once_with(timeout=15, max_retries=0)
        client.vector_stores.search.assert_called_once_with(
            vector_store_id='vs-pinned', query='readiness', max_num_results=3)
        project.toolboxes.invoke_latest_toolbox_mcp.assert_not_called()
        result = knowledge_query(project, 'vs-pinned', 'readiness', [])
        self.assertEqual(result['result'], {})
        self.assertEqual(result['citations'], [])

    def test_knowledge_read_is_bounded_and_provider_failures_propagate(self):
        project = MagicMock()
        client = project.get_openai_client.return_value.__enter__.return_value
        client.vector_stores.search.return_value = SimpleNamespace(data=[
            VectorStoreSearchResponse(file_id='file-actual', filename='runbook.md', score=0.9,
                attributes=None, content=[{'type': 'text', 'text': 'x' * 16384}]),
        ])
        with self.assertRaisesRegex(ValueError, 'byte budget'):
            knowledge_query(project, 'vs-pinned', 'readiness', [{'file_id': 'file-actual'}])
        client.vector_stores.search.side_effect = RuntimeError('native search unavailable')
        with self.assertRaisesRegex(RuntimeError, 'native search unavailable'):
            knowledge_query(project, 'vs-pinned', 'readiness', [])
        project.get_openai_client.reset_mock()
        for query in ('', 'x' * 1025):
            with self.assertRaises(ValueError):
                knowledge_query(project, 'vs-pinned', query, [])
        project.get_openai_client.assert_not_called()

    def test_memory_definition_retention_and_extraction_are_explicit(self):
        project = Mock()
        with self.assertRaises(ValueError):
            ensure_memory(project, 'name', 'chat', 'embedding', retention_days=0)
        project.beta.memory_stores.get.assert_not_called()

    def test_partial_cleanup_retries_and_rejects_foreign_files_before_mutation(self):
        from azure.core.exceptions import ResourceNotFoundError
        from portfolio_agent_runtime.knowledge import cleanup
        manifest = {'toolbox': 'demo-revision', 'vector_store': 'vector',
                    'repository': 'example/repo', 'revision': 'a' * 40, 'corpus_sha256': 'b' * 64,
                    'sources': [{'file_id': 'file-owned', 'sha256': 'c' * 64}]}
        project, openai = Mock(), Mock()
        project.toolboxes.get.side_effect = ResourceNotFoundError('already deleted')
        openai.vector_stores.retrieve.return_value = SimpleNamespace(metadata={
            'repository': manifest['repository'], 'revision': manifest['revision'],
            'corpus_sha256': manifest['corpus_sha256']})
        openai.files.retrieve.return_value = SimpleNamespace(id='file-owned', filename='foreign.md')
        with self.assertRaises(ValueError):
            cleanup(project, openai, manifest)
        project.toolboxes.delete.assert_not_called()
        openai.vector_stores.delete.assert_not_called()
        openai.files.retrieve.return_value.filename = 'demo-revision-' + 'c' * 64 + '.md'
        cleanup(project, openai, manifest)
        openai.files.delete.assert_called_once_with('file-owned')


class HostingTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_evidence_never_opens_persistent_context(self):
        from portfolio_agent_runtime.hosting import invoke
        with patch('portfolio_agent_runtime.hosting.user_context') as factory:
            with self.assertRaises(ValueError):
                await invoke({'incident': {'summary': 'replay'}, 'evidence': {}}, Mock(),
                             agent='demo', project=Mock(), memory_store='memory')
            factory.assert_not_called()

    async def test_history_stays_separate_and_checkpoint_contains_no_raw_incident(self):
        from unittest.mock import MagicMock
        from portfolio_agent_runtime.hosting import invoke
        context = Mock()
        context.recall = AsyncMock(return_value=[{'id': 'memory-owned', 'content': 'Historical advice'}])
        context.checkpoint = AsyncMock()
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=context)
        manager.__aexit__ = AsyncMock(return_value=False)
        investigate = Mock(return_value={'diagnosis': {'decision': 'abstain', 'answer': 'More evidence is needed.', 'evidence': []},
                                         'trace_id': 'trace', 'deployment': {'version': '1'}})
        with patch('portfolio_agent_runtime.hosting.user_context', return_value=manager):
            result = await invoke({'incident': {'summary': 'Investigate current readiness'}, 'investigation_id': 'case-1'},
                                  investigate, agent='demo', project=Mock(), memory_store='memory')
        body, history = investigate.call_args.args
        self.assertNotIn('memories', body['incident'])
        self.assertEqual(history['memories'][0]['id'], 'memory-owned')
        saved = context.checkpoint.call_args.args[1]
        self.assertNotIn('incident', saved)
        bounded(saved)
        self.assertEqual(result['investigation_id'], 'case-1')


class PublicationTests(unittest.TestCase):
    def test_completed_publication_is_reused_only_with_matching_sources_and_provenance(self):
        import hashlib
        from portfolio_agent_runtime.knowledge import publish
        project, openai = Mock(), Mock()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'runbook.md').write_text('Reviewed operational knowledge')
            data = corpus(directory)
            fingerprint = hashlib.sha256(json.dumps(
                [(d['path'], d['sha256']) for d in data], separators=(',', ':')).encode()).hexdigest()
            project.toolboxes.get.return_value = SimpleNamespace(default_version='1')
            version = SimpleNamespace(metadata={'repository': 'example/repo', 'corpus_sha256': fingerprint,
                                                'source_path': 'agents/demo/knowledge'},
                                      tools=[SimpleNamespace(vector_store_ids=['vector-owned'])])
            project.toolboxes.get_version.return_value = version
            openai.vector_stores.files.list.return_value = [SimpleNamespace(
                id='file-owned', status='completed', attributes={
                    'path': 'runbook.md', 'sha256': data[0]['sha256'], 'url': 'https://github.com/example/repo/blob/commit/runbook.md'})]
            manifest = publish(project, openai, directory, 'a' * 63, 'b' * 40, 'example/repo', 'agents/demo/knowledge')
            self.assertLessEqual(len(manifest['toolbox']), 63)
            self.assertEqual(manifest['sources'][0]['file_id'], 'file-owned')
            project.toolboxes.create_version.assert_not_called()
            openai.files.create.assert_not_called()
            version.metadata['corpus_sha256'] = 'foreign'
            with self.assertRaises(ValueError):
                publish(project, openai, directory, 'a' * 63, 'b' * 40, 'example/repo', 'agents/demo/knowledge')


class ForgetIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_forget_cannot_delete_another_callers_memory(self):
        memories = Mock()
        memories.get_memory.return_value = SimpleNamespace(scope='another-caller')
        context = Context(Mock(), memories, 'memory', 'own-caller')
        with self.assertRaises(ValueError):
            await context.forget('mem-other')
        memories.delete_memory.assert_not_called()
