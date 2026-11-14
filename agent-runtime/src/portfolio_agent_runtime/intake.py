"""Native durable event UID deduplication, leases and a bounded intake rate."""
from contextlib import suppress
import hashlib
import os
import time

from azure.ai.agentserver.core import get_request_context
from azure.ai.agentserver.core.storage import FoundryStateStore, FoundryStorageConflictError, FoundryStoragePreconditionError

from .context import bounded, identifier


async def investigate_event(payload, investigate, *, agent, namespace, workload):
    if (not os.getenv('FOUNDRY_HOSTING_ENVIRONMENT') or not get_request_context().call_id or not get_request_context().user_id
            or set(payload) != {'operation', 'event_uid', 'namespace', 'workload', 'delivery_id'}
            or payload['namespace'] != namespace or payload['workload'] != workload):
        raise ValueError('Event must match the configured investigation target and hosted invocation')
    uid = identifier(payload['event_uid'])
    delivery_id = identifier(payload['delivery_id'])
    key = 'event-' + hashlib.sha256(f'{namespace}/{workload}/{uid}'.encode()).hexdigest()
    store = await FoundryStateStore.get_or_create(f'{identifier(agent)}/events-v1',
                                                 user_isolation=True, item_ttl_seconds=86400, connection_timeout=5, read_timeout=15, retry_total=0)
    try:
        properties = await store.get()
        if not properties.user_isolation or properties.item_ttl_seconds != 86400:
            raise ValueError('Existing event store has incompatible isolation or retention')
        now = time.time()
        claim = {'status': 'running', 'lease_until': now + 180}
        try:
            owned = await store.create_item(key, claim)
        except FoundryStorageConflictError:
            item = await store.get_item(key)
            if item.value['status'] == 'complete':
                return {**item.value['result'], 'duplicate': True}
            if item.value['status'] == 'running' and item.value['lease_until'] > now:
                return {'status': 'in_progress', 'duplicate': True}
            try:
                owned = await store.set_item(key, claim, if_match=item.etag)
            except FoundryStoragePreconditionError:
                return {'status': 'in_progress', 'duplicate': True}
        try:
            minute = 'rate-' + str(int(now // 60))
            allowed = False
            for _ in range(3):
                try:
                    await store.create_item(minute, {'count': 1})
                    allowed = True
                    break
                except FoundryStorageConflictError:
                    bucket = await store.get_item(minute)
                    if bucket.value['count'] >= 2:
                        break
                    try:
                        await store.set_item(minute, {'count': bucket.value['count'] + 1}, if_match=bucket.etag)
                        allowed = True
                        break
                    except FoundryStoragePreconditionError:
                        continue
            if not allowed:
                await store.set_item(key, {'status': 'failed', 'lease_until': 0}, if_match=owned.etag)
                return {'status': 'rate_limited', 'retry_after_seconds': 60}
            # Event messages never enter the model. Only approved target metadata
            # and a fixed instruction are passed to the existing live collectors.
            incident = {'namespace': namespace, 'workload': workload, 'summary':
                'A Kubernetes readiness event was reported. Investigate current state; '
                'do not assume an ongoing fault or recovery from the event alone.'}
            result = await investigate({'incident': incident})
            result['delivery_id'] = delivery_id
            await store.set_item(key, {'status': 'complete', 'result': bounded({key: value for key, value in result.items() if key != 'context'}, 16384)},
                                 if_match=owned.etag)
            return result
        except BaseException:
            # A replacement claimant owns a new ETag. Neither a late result nor
            # failure cleanup may overwrite it; preserve the original exception.
            with suppress(FoundryStoragePreconditionError):
                await store.set_item(key, {'status': 'failed', 'lease_until': 0}, if_match=owned.etag)
            raise
    finally:
        await store.aclose()
