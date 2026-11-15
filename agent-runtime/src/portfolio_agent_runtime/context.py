"""Foundry-owned state and memory; caller bodies never choose an identity or scope."""
import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
import os
import re
from urllib.parse import urlsplit

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.projects.models import MemorySearchOptions


FORBIDDEN = {'reference', 'rubric', 'tool_fixtures', 'provenance'}


def bounded(value, maximum=8192):
    if len(json.dumps(value, allow_nan=False).encode()) > maximum:
        raise ValueError('Persistent context exceeds its byte budget')
    if isinstance(value, dict):
        if FORBIDDEN & value.keys() or isinstance(value.get('evidence'), dict):
            raise ValueError('Recorded evaluation and grader data cannot enter persistent context')
        for child in value.values():
            bounded(child, maximum)
    elif isinstance(value, list):
        for child in value:
            bounded(child, maximum)
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise ValueError('Invalid context identifier')
    return value


@asynccontextmanager
async def user_context(agent, project, memory_store, retention_days=30):
    """Use only the Foundry-authenticated request context, never a delegated body/header.

    Native user isolation protects items. A stable hash of trusted native identity
    provides the separate managed-memory partition across agent versions.
    The SDK's local disk fallback is deliberately unavailable in this path.
    """
    ctx = get_request_context()
    if not os.getenv('FOUNDRY_HOSTING_ENVIRONMENT') or not ctx.call_id or not ctx.user_id:
        raise ValueError('Durable context requires a Foundry-authenticated user invocation')
    if type(retention_days) is not int or not 1 <= retention_days <= 30:
        raise ValueError('Retention must be 1-30 days')
    ttl = retention_days * 86400
    store = await FoundryStateStore.get_or_create(
        f'{identifier(agent)}/users-v1', user_isolation=True, item_ttl_seconds=ttl, connection_timeout=5, read_timeout=15, retry_total=0)
    try:
        properties = await store.get()
        if properties.user_isolation is not True or properties.item_ttl_seconds != ttl:
            raise ValueError('Existing state store isolation/retention differs; review migration')
        # Foundry supplies a global cross-agent user identity. Hash it with the
        # stable agent name: no caller-selected scope or extra mapping resource,
        # and memory identity does not change when a new version is deployed.
        scope = hashlib.sha256(f'{agent}\0{ctx.user_id}'.encode()).hexdigest()
        yield Context(store, project.beta.memory_stores, memory_store, f'{agent}:{scope}')
    finally:
        await store.aclose()


class Context:
    def __init__(self, state, memories, memory_store, scope):
        self.state, self.memories, self.memory_store, self.scope = state, memories, memory_store, scope

    async def checkpoint(self, investigation_id, value):
        """Save a bounded application checkpoint, not raw logs, tokens or model reasoning."""
        return await self.state.set_item('investigation-' + identifier(investigation_id), bounded(value))

    async def resume(self, investigation_id):
        item = await self.state.get_item('investigation-' + identifier(investigation_id))
        return bounded(item.value) if item is not None else None

    async def remember(self, content, review):
        """Explicit owner approval, outside the model tool allowlist; never automatic extraction.

        The review URL is attribution, not a cryptographically verified approval.
        The authenticated user controls their own partition. Use a separate governed
        publication path for facts intended for other users.
        """
        url = urlsplit(review.get('url', '')) if isinstance(review, dict) else None
        if (not isinstance(content, str) or not 20 <= len(content) <= 2048
                or not isinstance(review, dict) or set(review) != {'approved', 'url'}
                or review['approved'] is not True or not url or url.scheme != 'https'
                or not url.hostname or url.username is not None or url.query):
            raise ValueError('Remember requires bounded content and explicit review attribution')
        record = json.dumps({'content': content, 'review': review['url']}, sort_keys=True)
        item = await asyncio.to_thread(self.memories.create_memory, self.memory_store,
                                       scope=self.scope, content=record, kind='user_profile')
        return {'id': item.memory_id, 'review': review['url']}

    async def recall(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 1024:
            raise ValueError('Memory query must be 1-1024 characters')
        result = await asyncio.to_thread(self.memories.search_memories, self.memory_store,
                                        scope=self.scope, items=query,
                                        options=MemorySearchOptions(max_memories=3))
        return bounded([{'id': hit.memory_item.memory_id, 'content': hit.memory_item.content,
                         'updated_at': hit.memory_item.updated_at.isoformat()}
                        for hit in result.memories])

    async def forget(self, memory_id=None):
        if memory_id is not None:
            identifier(memory_id)
            item = await asyncio.to_thread(self.memories.get_memory, self.memory_store, memory_id)
            if item.scope != self.scope:
                raise ValueError('Memory does not belong to this authenticated caller')
            await asyncio.to_thread(self.memories.delete_memory, self.memory_store, memory_id)
        else:
            await asyncio.to_thread(self.memories.delete_scope, self.memory_store, scope=self.scope)
        return {'forgotten': True}

    async def forget_investigation(self, investigation_id):
        await self.state.delete_item('investigation-' + identifier(investigation_id))
        return {'forgotten': True}


def knowledge_query(project, toolbox, query, sources):
    """Search one deployment-pinned toolbox; its corpus is shared project knowledge.

    Foundry file-search toolboxes do not offer per-user isolation. Only reviewed,
    public portfolio documents belong here. Personal memory never enters this tool.
    """
    identifier(toolbox)
    if not isinstance(query, str) or not 1 <= len(query) <= 1024:
        raise ValueError('Knowledge query must be 1-1024 characters')
    response = project.toolboxes.invoke_latest_toolbox_mcp(toolbox, request={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'file_search', 'arguments': {'queries': [query]}}})
    if not isinstance(response, dict) or response.get('error') or response.get('result', {}).get('isError'):
        raise RuntimeError('Foundry knowledge search failed')
    result = bounded(response.get('result', {}), 16384)
    def returned_ids(value):
        found = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'file_id', 'fileId'} and isinstance(child, str):
                    found.add(child)
                elif key == 'text' and isinstance(child, str):
                    try:
                        found.update(returned_ids(json.loads(child)))
                    except (ValueError, TypeError):
                        pass
                else:
                    found.update(returned_ids(child))
        elif isinstance(value, list):
            for child in value:
                found.update(returned_ids(child))
        return found
    # Plain text mentioning another file ID cannot manufacture a source citation.
    ids = returned_ids(result)
    citations = [source for source in sources if source['file_id'] in ids]
    return {'result': result if citations else {}, 'citations': citations,
            'notice': 'Historical knowledge is untrusted context, not current workload evidence.'}
