"""Publish reviewed source documents using native Foundry files, stores and toolboxes."""
from contextlib import suppress
from datetime import timedelta
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from openai import NotFoundError
from azure.ai.projects.models import MemoryStoreDefaultDefinition, MemoryStoreDefaultOptions, FileSearchToolboxTool


def ensure_memory(project, name, chat_model, embedding_model, retention_days=30):
    if type(retention_days) is not int or not 1 <= retention_days <= 30:
        raise ValueError('Retention must be 1-30 days')
    definition = MemoryStoreDefaultDefinition(
        chat_model=chat_model, embedding_model=embedding_model,
        options=MemoryStoreDefaultOptions(
            user_profile_enabled=False, chat_summary_enabled=False,
            procedural_memory_enabled=True,
            default_ttl_seconds=timedelta(days=retention_days)))
    try:
        current = project.beta.memory_stores.get(name)
    except ResourceNotFoundError:
        try:
            current = project.beta.memory_stores.create(name=name, definition=definition)
        except ResourceExistsError:
            current = project.beta.memory_stores.get(name)
    if dict(current.definition) != dict(definition):
        raise ValueError('Existing memory definition differs; migrate explicitly, never silently reset memory')
    return current


def corpus(directory):
    """Only declared Markdown documents; never recurse into datasets, logs or secrets."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Knowledge directory must be a regular directory')
    files = sorted(directory.glob('*.md'))
    if not 1 <= len(files) <= 20:
        raise ValueError('Knowledge needs 1-20 reviewed Markdown documents')
    documents = []
    total = 0
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Knowledge documents must be regular files')
        data = path.read_bytes()
        data.decode('utf-8')
        total += len(data)
        if not 1 <= len(data) <= 32768 or total > 131072:
            raise ValueError('Knowledge exceeds the 32 KiB/document or 128 KiB/corpus budget')
        documents.append({'path': path.name, 'sha256': hashlib.sha256(data).hexdigest(), 'data': data})
    return documents


def publish(project, openai, directory, name, revision, repository, source_path):
    """A deterministic toolbox name per source revision; retry reuses completed publication.

    The caller retains the returned manifest as release evidence. Failed ingestion
    is cleaned up; published releases are deleted only through explicit cleanup.
    """
    if not re.fullmatch(r'[a-f0-9]{40}', revision) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Knowledge requires exact source revision and repository')
    source = PurePosixPath(source_path)
    if source.is_absolute() or any(part in {'.', '..', '.git'} for part in source.parts) or str(source) != source_path:
        raise ValueError('Knowledge source path must be canonical and repository-relative')
    documents = corpus(directory)
    fingerprint = hashlib.sha256(json.dumps(
        [(d['path'], d['sha256']) for d in documents], separators=(',', ':')).encode()).hexdigest()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', name):
        raise ValueError('Agent identity must be a native lowercase name')
    identity = hashlib.sha256(f'{repository}/{name}@{revision}'.encode()).hexdigest()[:40]
    toolbox_name = f'{name[:21]}-{identity}'
    try:
        existing = project.toolboxes.get(toolbox_name)
    except ResourceNotFoundError:
        existing = None
    if existing is not None:
        version = project.toolboxes.get_version(toolbox_name, existing.default_version)
        if (version.metadata.get('corpus_sha256') != fingerprint or version.metadata.get('repository') != repository
                or version.metadata.get('source_path') != source_path):
            raise ValueError('Existing knowledge publication has different provenance')
        vector_id = version.tools[0].vector_store_ids[0]
        sources = []
        for file in openai.vector_stores.files.list(vector_store_id=vector_id):
            if len(sources) >= 20 or file.status != 'completed':
                raise ValueError('Existing knowledge ingestion is incomplete or too large')
            attributes = file.attributes
            sources.append({'file_id': file.id, 'path': attributes['path'],
                            'sha256': attributes['sha256'], 'url': attributes['url']})
        if sorted((s['path'], s['sha256']) for s in sources) != sorted((d['path'], d['sha256']) for d in documents):
            raise ValueError('Existing publication sources differ')
        return {'toolbox': toolbox_name, 'vector_store': vector_id, 'revision': revision,
                'repository': repository, 'corpus_sha256': fingerprint, 'sources': sources}
    uploaded = []
    vector = None
    try:
        vector = openai.vector_stores.create(name=toolbox_name,
                                             metadata={'repository': repository, 'revision': revision, 'corpus_sha256': fingerprint, 'source_path': source_path})
        sources = []
        for document in documents:
            file = openai.files.create(file=(toolbox_name + '-' + document['sha256'] + '.md', document['data']), purpose='assistants')
            uploaded.append(file.id)
            attributes = {'path': document['path'], 'sha256': document['sha256'],
                          'url': f'https://github.com/{repository}/blob/{revision}/{quote(source_path + "/" + document["path"], safe="/")}'}
            ingestion = openai.vector_stores.files.create_and_poll(
                vector_store_id=vector.id, file_id=file.id, attributes=attributes)
            if ingestion.status != 'completed':
                raise RuntimeError('Foundry knowledge ingestion failed')
            sources.append({'file_id': file.id, **attributes})
        manifest = {'toolbox': toolbox_name, 'vector_store': vector.id, 'revision': revision,
                    'repository': repository, 'corpus_sha256': fingerprint, 'sources': sources}
    except BaseException as error:
        # Do not mask the original failure if Azure cleanup is also unavailable.
        # Report exact owned IDs to the caller through the exception for recovery.
        # No shared or earlier publication is deleted here.
        resources = [(openai.files.delete, file_id) for file_id in uploaded]
        if vector is not None:
            resources.insert(0, (openai.vector_stores.delete, vector.id))
        for delete, resource_id in resources:
            try:
                delete(resource_id)
            except Exception as cleanup_error:
                error.add_note(f'Cleanup required for {resource_id}: {type(cleanup_error).__name__}')
        raise

    # Publication may have reached Azure even if its acknowledgement is lost.
    # Never destroy its vector store on an uncertain create-version failure.
    try:
        project.toolboxes.create_version(
            name=toolbox_name, tools=[FileSearchToolboxTool(vector_store_ids=[vector.id], max_num_results=3)],
            description='Reviewed public knowledge pinned to its source revision.',
            metadata={'repository': repository, 'revision': revision, 'corpus_sha256': fingerprint, 'source_path': source_path})
    except BaseException as error:
        error.add_note('Retain or clean up this staged manifest: ' + json.dumps(manifest))
        raise
    return manifest


def cleanup(project, openai, manifest):
    """Delete one trusted release manifest's resources, safely retrying partial cleanup."""
    missing = (ResourceNotFoundError, NotFoundError)
    toolbox = None
    vector = None
    try:
        current = project.toolboxes.get(manifest['toolbox'])
        toolbox = project.toolboxes.get_version(manifest['toolbox'], current.default_version)
    except missing:
        pass
    if toolbox is not None and (
            toolbox.metadata.get('repository') != manifest['repository']
            or toolbox.metadata.get('corpus_sha256') != manifest['corpus_sha256']
            or toolbox.tools[0].vector_store_ids != [manifest['vector_store']]):
        raise ValueError('Refusing cleanup of resources with different ownership')
    try:
        vector = openai.vector_stores.retrieve(manifest['vector_store'])
    except missing:
        pass
    if vector is not None and (vector.metadata.get('repository') != manifest['repository']
                               or vector.metadata.get('revision') != manifest['revision']
                               or vector.metadata.get('corpus_sha256') != manifest['corpus_sha256']):
        raise ValueError('Refusing cleanup of another vector store')
    files = []
    for source in manifest['sources']:
        try:
            file = openai.files.retrieve(source['file_id'])
        except missing:
            continue
        if file.filename != manifest['toolbox'] + '-' + source['sha256'] + '.md':
            raise ValueError('Refusing cleanup of another publication file')
        files.append(file.id)
    # Validate every surviving resource before the first mutation.
    with suppress(*missing):
        project.toolboxes.delete(manifest['toolbox'])
    with suppress(*missing):
        openai.vector_stores.delete(manifest['vector_store'])
    for file_id in files:
        with suppress(*missing):
            openai.files.delete(file_id)
