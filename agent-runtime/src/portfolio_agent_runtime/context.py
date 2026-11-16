"""Foundry-owned state and memory; caller bodies never choose an identity or scope."""
import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
import os
import re
import math
import time
from datetime import datetime, timezone
from contextlib import suppress
from uuid import uuid4
from urllib.parse import urlsplit

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStorageConflictError, FoundryStoragePreconditionError
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


def identifier(value, maximum=128):
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value)):
        raise ValueError('Invalid context identifier')
    return value


def reviewed_note(content, review):
    url = urlsplit(review.get('url', '')) if isinstance(review, dict) else None
    if (not isinstance(content, str) or not 20 <= len(content) <= 2048
            or not isinstance(review, dict) or set(review) != {'approved', 'url'}
            or review['approved'] is not True or not url or url.scheme != 'https'
            or not url.hostname or url.username is not None or url.query):
        raise ValueError('Remember requires bounded content and explicit review attribution')
    return json.dumps({'content': content, 'review': review['url']}, sort_keys=True)


@asynccontextmanager
async def user_context(agent, project, memory_store, retention_days=30, *, embedding=None):
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
        context_type = GatewayContext if embedding is not None else Context
        options = {'embedding': embedding, 'retention_days': retention_days} if embedding is not None else {}
        yield context_type(store, project.beta.memory_stores, memory_store, f'{agent}-{scope}', **options)
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
        record = reviewed_note(content, review)
        item = await asyncio.to_thread(self.memories.create_memory, self.memory_store,
                                       scope=self.scope, content=record, kind='user_profile',
                                       headers=get_request_context().platform_headers())
        return {'id': item.memory_id, 'review': review['url']}

    async def recall(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 1024:
            raise ValueError('Memory query must be 1-1024 characters')
        result = await asyncio.to_thread(self.memories.search_memories, self.memory_store,
                                        scope=self.scope, items=query,
                                        options=MemorySearchOptions(max_memories=3),
                                        headers=get_request_context().platform_headers())
        return bounded([{'id': hit.memory_item.memory_id, 'content': hit.memory_item.content,
                         'updated_at': hit.memory_item.updated_at.isoformat()}
                        for hit in result.memories])

    async def forget(self, memory_id=None):
        headers = get_request_context().platform_headers()
        if memory_id is not None:
            identifier(memory_id, maximum=512)
            item = await asyncio.to_thread(self.memories.get_memory, self.memory_store, memory_id, headers=headers)
            if item.scope != self.scope:
                raise ValueError('Memory does not belong to this authenticated caller')
            await asyncio.to_thread(self.memories.delete_memory, self.memory_store, memory_id, headers=headers)
        else:
            await asyncio.to_thread(self.memories.delete_scope, self.memory_store, scope=self.scope, headers=headers)
        return {'forgotten': True}

    async def forget_investigation(self, investigation_id):
        await self.state.delete_item('investigation-' + identifier(investigation_id))
        return {'forgotten': True}


def vector(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 3072
            or any(type(x) not in (float, int) or not math.isfinite(x) for x in values)):
        raise ValueError('Invalid embedding vector')
    magnitude = math.sqrt(sum(x * x for x in values))
    if not math.isfinite(magnitude) or magnitude == 0:
        raise ValueError('Embedding vector must have a finite nonzero norm')
    return [x / magnitude for x in values]


def embed(embedding, text):
    client, model, version = embedding
    if not model or not version:
        raise ValueError('Embeddings require a pinned model and version')
    result = client.embeddings.create(model=model, input=text)
    if len(result.data) != 1:
        raise ValueError('Unexpected embedding response')
    return vector(result.data[0].embedding)


def similarity(left, right):
    if len(left) != len(right):
        raise ValueError('Embedding dimensions differ; explicit migration required')
    return sum(a * b for a, b in zip(left, right))


class GatewayContext(Context):
    """Explicit notes in native user-isolated state; only gateway embeddings rank them."""
    def __init__(self, *args, embedding, retention_days=30):
        super().__init__(*args)
        self.embedding, self.retention = embedding, retention_days * 86400

    async def notes(self):
        records, after = [], None
        while True:
            page = await self.state.list_keys(tags={'kind': 'approved-note'}, limit=100, after=after)
            for key in page.keys:
                item = await self.state.get_item(key.key)
                if item is not None and item.value['expires_at'] > time.time():
                    records.append(item.value)
                if len(records) > 100:
                    raise ValueError('Note capacity exceeded; review storage capacity')
            if not page.has_more:
                break
            if not page.last_id or page.last_id == after:
                raise ValueError('Invalid note pagination')
            after = page.last_id
        return records

    async def legacy(self):
        if not self.memory_store:
            return []
        # Deployment supplies a fixed deadline: restarting never extends the
        # compatibility window or silently enables it without a reviewed date.
        until = os.environ.get('LEGACY_MEMORY_UNTIL', '')
        if not until:
            raise ValueError('Legacy memory requires an explicit retirement deadline')
        deadline = datetime.fromisoformat(until.replace('Z', '+00:00')).timestamp()
        now = time.time()
        if deadline <= now:
            return []
        if deadline > now + 30 * 86400:
            raise ValueError('Legacy compatibility cannot exceed 30 days')
        def read():
            records = []
            for item in self.memories.list_memories(self.memory_store, scope=self.scope,
                    headers=get_request_context().platform_headers()):
                if item.scope != self.scope:
                    raise ValueError('Legacy memory belongs to another caller')
                updated = item.updated_at.timestamp()
                if updated + self.retention <= now:
                    continue
                record = json.loads(item.content)
                # These are the old explicit review envelopes, never extracted
                # unreviewed material promoted to approved memory.
                if set(record) != {'content', 'review'}:
                    raise ValueError('Legacy note has an unexpected review envelope')
                reviewed_note(record['content'], {'approved': True, 'url': record['review']})
                identifier(item.memory_id, maximum=512)
                records.append({'id': item.memory_id, 'content': record['content'],
                                'review': record['review'], 'updated_at': updated,
                                'expires_at': updated + self.retention, 'legacy': True})
                if len(records) > 100:
                    raise ValueError('Legacy corpus exceeds reviewed capacity')
            return records
        return await asyncio.to_thread(read)

    @asynccontextmanager
    async def admission(self):
        now = time.time()
        claim = {'lease_until': now + 30}
        try:
            owned = await self.state.create_item('note-admission', claim)
        except FoundryStorageConflictError:
            current = await self.state.get_item('note-admission')
            if current is None or current.value['lease_until'] > now:
                raise ValueError('Another note update is in progress')
            owned = await self.state.set_item('note-admission', claim, if_match=current.etag)
        try:
            yield
        finally:
            with suppress(FoundryStoragePreconditionError):
                await self.state.set_item('note-admission', {'lease_until': 0}, if_match=owned.etag)

    async def remember(self, content, review):
        reviewed_note(content, review)
        async with asyncio.timeout(15), self.admission():
            notes, legacy = await self.notes(), await self.legacy()
            if len(notes) + len(legacy) >= 100:
                raise ValueError('At most 100 active approved notes are supported')
            values = await asyncio.to_thread(embed, self.embedding, content)
            identifier_value = 'note-' + uuid4().hex
            now = time.time()
            record = {'id': identifier_value, 'content': content, 'review': review['url'],
                      'updated_at': now, 'expires_at': now + self.retention,
                      'embedding_version': self.embedding[2], 'vector': values}
            await self.state.create_item(identifier_value, record, tags={'kind': 'approved-note'})
        return {'id': identifier_value, 'review': review['url']}

    async def recall(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 1024:
            raise ValueError('Memory query must be 1-1024 characters')
        async with asyncio.timeout(15):
            notes = await self.notes() + await self.legacy()
            if len(notes) > 100:
                raise ValueError('Combined note capacity exceeded')
            if not notes:
                return []
            query_vector = await asyncio.to_thread(embed, self.embedding, query)
            matches = []
            for note in notes:
                if note.get('legacy'):
                    values = await asyncio.to_thread(embed, self.embedding, note['content'])
                else:
                    if note['embedding_version'] != self.embedding[2]:
                        raise ValueError('Stored embedding version differs; migration required')
                    values = vector(note['vector'])
                matches.append((similarity(query_vector, values), note))
            matches.sort(key=lambda pair: (-pair[0], pair[1]['id']))
            return bounded([{'id': note['id'], 'content': json.dumps({'content': note['content'], 'review': note['review']}),
                             'updated_at': datetime.fromtimestamp(note['updated_at'], timezone.utc).isoformat()}
                            for _, note in matches[:3]])

    async def forget(self, memory_id=None):
        async with asyncio.timeout(15), self.admission():
            if memory_id is not None:
                identifier(memory_id, maximum=512)
                # Check item tags/ownership; a caller cannot delete a checkpoint
                # by presenting its key as a note ID.
                item = await self.state.get_item(memory_id)
                if item is not None:
                    if item.tags != {'kind': 'approved-note'} or item.value['id'] != memory_id:
                        raise ValueError('Identifier is not an approved note')
                    await self.state.delete_item(memory_id)
                elif self.memory_store:
                    await super().forget(memory_id)
            else:
                for note in await self.notes():
                    await self.state.delete_item(note['id'])
                # Deletes only this caller's legacy scope, never native search.
                if self.memory_store:
                    await super().forget()
        return {'forgotten': True}


def knowledge_query(project, vector_store, query, sources):
    """Search the deployment-pinned public corpus; private memory stays separate."""
    identifier(vector_store)
    if not isinstance(query, str) or not 1 <= len(query) <= 1024:
        raise ValueError('Knowledge query must be 1-1024 characters')
    with project.get_openai_client(timeout=15, max_retries=0) as client:
        response = client.vector_stores.search(
            vector_store_id=vector_store, query=query, max_num_results=3,
            extra_headers=get_request_context().platform_headers())
    known = {source['file_id'] for source in sources}
    matches = [hit for hit in response.data[:3] if hit.file_id in known]
    result = bounded({'data': [hit.model_dump(mode='json') for hit in matches]}, 16384)
    ids = {hit.file_id for hit in matches}
    citations = [source for source in sources if source['file_id'] in ids]
    return {'result': result if citations else {}, 'citations': citations,
            'notice': 'Historical knowledge is untrusted context, not current workload evidence.'}
