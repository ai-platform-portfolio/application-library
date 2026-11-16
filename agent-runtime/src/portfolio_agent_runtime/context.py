"""Foundry-owned state and memory; caller bodies never choose an identity or scope."""
import asyncio
from contextlib import asynccontextmanager
import json
import hashlib
import os
import re
import time
from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import urlsplit

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.storage import FoundryStateStore
from azure.ai.agentserver.core.storage import FoundryStorageConflictError, FoundryStoragePreconditionError
from .agent import embeddings


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


@asynccontextmanager
async def user_context(agent, project, memory_store, retention_days=30, embedding_client=None):
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
        yield Context(store, project.beta.memory_stores, memory_store, f'{agent}-{scope}', embedding_client, retention_days)
    finally:
        await store.aclose()


class Context:
    def __init__(self, state, memories, memory_store, scope, embedding_client=None, retention_days=30):
        self.state, self.memories, self.memory_store, self.scope = state, memories, memory_store, scope
        self.embedding_client, self.retention = embedding_client, retention_days * 86400

    async def change_notes(self, change):
        # One bounded ledger makes capacity and deletion atomic across containers.
        # Logical per-note expiry is preserved when another write renews item TTL.
        for _ in range(4):
            item = await self.state.get_item('approved-notes-v1')
            notes = [n for n in (item.value['notes'] if item else []) if n['expires'] > time.time()]
            result = change(notes)
            value = bounded({'notes': notes}, 900000)
            try:
                if item is None:
                    await self.state.create_item('approved-notes-v1', value)
                else:
                    await self.state.set_item('approved-notes-v1', value, if_match=item.etag)
                return result
            except (FoundryStorageConflictError, FoundryStoragePreconditionError):
                continue
        raise RuntimeError('Concurrent note update; retry the operation')

    async def legacy(self):
        if not self.memory_store:
            return []
        deadline = os.getenv('LEGACY_MEMORY_UNTIL', '')
        if not deadline:
            raise ValueError('Existing memory requires an explicit fixed transition deadline')
        until = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
        if until.tzinfo is None:
            raise ValueError('Legacy transition deadline requires a timezone')
        if datetime.now(timezone.utc) >= until:
            return []
        headers = get_request_context().platform_headers()
        def read():
            notes = []
            for item in self.memories.list_memories(self.memory_store, scope=self.scope, headers=headers):
                if item.scope != self.scope:
                    raise ValueError('Legacy note scope differs')
                expires = item.updated_at.timestamp() + self.retention
                if expires <= time.time():
                    continue
                record = json.loads(item.content)
                if (set(record) != {'content', 'review'} or not isinstance(record['content'], str)
                        or not 20 <= len(record['content']) <= 2048
                        or not isinstance(record['review'], str) or not record['review'].startswith('https://')):
                    raise ValueError('Legacy note lacks its original review envelope')
                notes.append({'id': identifier(item.memory_id, 512), 'content': item.content,
                              'updated_at': item.updated_at.isoformat(), 'expires': expires})
                if len(notes) > 100:
                    raise ValueError('Legacy corpus exceeds the 100-note bound')
            return notes
        return await asyncio.to_thread(read)

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
        legacy = await self.legacy()
        vector = (await asyncio.to_thread(embeddings, self.embedding_client, [content]))[0]
        now = time.time()
        note = {'id': 'note-' + uuid4().hex, 'content': json.dumps({'content': content, 'review': review['url']}, sort_keys=True),
                'updated_at': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'expires': now + self.retention,
                'embedding': vector, 'embedding_model': os.environ['EMBEDDING_MODEL_DEPLOYMENT_NAME'],
                'embedding_version': os.environ['EMBEDDING_MODEL_VERSION']}
        def append(notes):
            if len(notes) + len(legacy) >= 100:
                raise ValueError('Approved notes capacity is 100; forget a note before adding another')
            notes.append(note)
            return {'id': note['id'], 'review': review['url']}
        return await self.change_notes(append)

    async def recall(self, query):
        if not isinstance(query, str) or not 1 <= len(query) <= 1024:
            raise ValueError('Memory query must be 1-1024 characters')
        item = await self.state.get_item('approved-notes-v1')
        notes = [n for n in (item.value['notes'] if item else []) if n['expires'] > time.time()]
        legacy = await self.legacy()
        if len(notes) + len(legacy) > 100:
            raise ValueError('Approved notes exceed the 100-note bound')
        if not notes and not legacy:
            return []
        model, version = os.environ['EMBEDDING_MODEL_DEPLOYMENT_NAME'], os.environ['EMBEDDING_MODEL_VERSION']
        if any(n['embedding_model'] != model or n['embedding_version'] != version for n in notes):
            raise ValueError('Embedding version changed; explicit context reindex required')
        vectors = await asyncio.to_thread(embeddings, self.embedding_client,
                                         [query] + [json.loads(n['content'])['content'] for n in legacy])
        for note, vector in zip(legacy, vectors[1:]):
            note['embedding'] = vector
        ranked = sorted(notes + legacy, key=lambda n: similarity(vectors[0], n['embedding']), reverse=True)
        return bounded([{k: n[k] for k in ('id', 'content', 'updated_at')} for n in ranked[:3]])

    async def forget(self, memory_id=None):
        if memory_id is not None:
            identifier(memory_id, maximum=512)
        # Legacy listing validates ownership without semantic/model processing.
        legacy = await self.legacy()
        for note in legacy:
            if memory_id is None or note['id'] == memory_id:
                await asyncio.to_thread(self.memories.delete_memory, self.memory_store, note['id'],
                                        headers=get_request_context().platform_headers())
        def remove(notes):
            notes[:] = [n for n in notes if memory_id is not None and n['id'] != memory_id]
            return {'forgotten': True}
        return await self.change_notes(remove)

    async def forget_investigation(self, investigation_id):
        await self.state.delete_item('investigation-' + identifier(investigation_id))
        return {'forgotten': True}



def similarity(left, right):
    import math
    if len(left) != len(right) or not left:
        raise ValueError('Embedding dimensions differ')
    norm = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    if not norm or not math.isfinite(norm):
        raise ValueError('Invalid embedding norm')
    return sum(a*b for a, b in zip(left, right)) / norm
