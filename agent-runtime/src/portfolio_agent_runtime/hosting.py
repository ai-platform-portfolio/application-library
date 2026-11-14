"""Compose native durable context with an application's synchronous investigator."""
import asyncio
from uuid import uuid4

from .context import bounded, knowledge_query, user_context


async def invoke(payload, investigate, *, agent, project, memory_store, retention_days=30, knowledge=None):
    """Replay fixtures bypass this entry point in the caller; reject them defensively."""
    if not isinstance(payload, dict) or 'evidence' in payload:
        raise ValueError('Persistent invocations cannot contain recorded evaluation evidence')
    operation = payload.get('operation', 'investigate')
    fields = {
        'investigate': {'operation', 'incident', 'investigation_id'},
        'remember': {'operation', 'content', 'review'},
        'recall': {'operation', 'query'},
        'forget': {'operation'},
        'resume': {'operation', 'investigation_id'},
        'forget_investigation': {'operation', 'investigation_id'},
    }
    if operation not in fields or not payload.keys() <= fields[operation]:
        raise ValueError('Unsupported persistent operation or fields')
    async with user_context(agent, project, memory_store, retention_days) as context:
        if operation == 'remember':
            return await context.remember(payload['content'], payload['review'])
        if operation == 'recall':
            return {'memories': await context.recall(payload['query'])}
        if operation == 'forget':
            return await context.forget()
        if operation == 'resume':
            return {'checkpoint': await context.resume(payload['investigation_id'])}
        if operation == 'forget_investigation':
            return await context.forget_investigation(payload['investigation_id'])
        incident = bounded(payload['incident'])
        if not isinstance(incident, dict) or not isinstance(incident.get('summary'), str):
            raise ValueError('Persistent investigation needs a bounded incident summary')
        query = incident['summary'][:1024]
        historical = {'memories': await context.recall(query)}
        if knowledge is not None:
            historical['knowledge'] = await asyncio.to_thread(
                knowledge_query, project, knowledge['toolbox'], query, knowledge['sources'])
        result = await asyncio.to_thread(investigate, {'incident': incident}, historical)
        # The application validates current tool evidence before this call returns.
        # Historical context is returned separately; it is never a diagnostic tool.
        investigation_id = payload.get('investigation_id') or uuid4().hex
        await context.checkpoint(investigation_id, {
            'diagnosis': result['diagnosis'], 'trace_id': result.get('trace_id'),
            'deployment': result.get('deployment'),
        })
        result['investigation_id'] = investigation_id
        result['context'] = historical
        return result
