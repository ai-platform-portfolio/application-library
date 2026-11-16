"""Native azd predeploy hook for reviewed knowledge and a stable memory store."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import ssl
import tempfile
from urllib.parse import urlsplit
from datetime import datetime, timezone, timedelta

from azure.ai.projects import AIProjectClient
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
import yaml

from .knowledge import ensure_memory, publish, gateway_manifest
from .agent import model_settings


def configure(path):
    config = yaml.safe_load(Path(path).read_text())
    connection = model_settings(config['model'])
    if config.get('connection_mode') == 'gateway' and connection['MODEL_CONNECTION_MODE'] != 'gateway':
        raise ValueError('This workload requires its declared gateway connection')
    memory = config.get('memory', {})
    environment = os.environ['AZURE_ENV_NAME']
    manifest = {}
    memory_name = ''
    if memory.get('enabled'):
        directory = Path(config['knowledge']['path'])
        if directory.is_absolute() or directory.is_symlink() or not directory.resolve().is_relative_to(Path.cwd().resolve()):
            raise ValueError('Knowledge must be inside the agent project')
        source_path = directory.resolve().relative_to(Path(os.environ['GITHUB_WORKSPACE']).resolve()).as_posix()
        repository, revision = os.environ['GITHUB_REPOSITORY'], os.environ['SOURCE_REVISION']
        if connection['MODEL_CONNECTION_MODE'] == 'gateway':
            memory_name = os.getenv('LEGACY_MEMORY_STORE_NAME', '')
            if memory_name:
                with DefaultAzureCredential() as credential, AIProjectClient(
                        endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'], credential=credential,
                        connection_timeout=5, read_timeout=15, retry_total=0) as project:
                    try:
                        project.beta.memory_stores.get(memory_name)
                    except ResourceNotFoundError:
                        memory_name = ''
                if memory_name:
                    deadline = datetime.fromisoformat(os.environ['LEGACY_MEMORY_UNTIL'].replace('Z', '+00:00'))
                    if deadline.tzinfo is None or deadline > datetime.now(timezone.utc) + timedelta(days=30):
                        raise ValueError('Declare a fixed legacy-memory retirement deadline within 30 days')
            manifest = gateway_manifest(directory, environment, revision, repository, source_path)
        else:
            memory_name = environment + '-memory'
            with DefaultAzureCredential() as credential, AIProjectClient(
                endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'], credential=credential,
                connection_timeout=5, read_timeout=15, retry_total=0) as project:
                ensure_memory(project, memory_name, memory.get('chat_model', config['model']), memory.get('embedding_model', 'embedding'),
                          memory.get('retention_days', 30))
                with project.get_openai_client() as openai:
                    manifest = publish(project, openai, directory, environment, revision, repository, source_path)
        # The caller uploads this with its existing release/evaluation artifacts.
        record = Path(os.environ['RUNNER_TEMP']) / 'acceptance' / 'knowledge.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(manifest, indent=2))
    values = {'MEMORY_STORE_NAME': memory_name,
              'KNOWLEDGE_MANIFEST_JSON': json.dumps(manifest, separators=(',', ':')),
              **connection,
              'METRICS_URL': os.getenv('METRICS_URL') or '',
              'METRICS_TOKEN_SCOPE': os.getenv('METRICS_TOKEN_SCOPE') or '',
              'AGENT_DIAGNOSTICS_BACKEND': os.getenv('AGENT_DIAGNOSTICS_BACKEND') or 'provided'}
    if connection['MODEL_CONNECTION_MODE'] == 'gateway':
        for capability in ('coding', 'embedding'):
            prefix = capability.upper() + '_'
            if not os.getenv(prefix + 'MODEL_DEPLOYMENT_NAME'):
                if capability == 'embedding' and memory.get('enabled'):
                    raise ValueError('Gateway memory requires explicit embedding configuration')
                continue
            selected = model_settings(capability=capability)
            values.update({prefix + 'MODEL_DEPLOYMENT_NAME': selected['MODEL_DEPLOYMENT_NAME'],
                           prefix + 'MODEL_API_KEY_SECRET_URI': selected['MODEL_API_KEY_SECRET_URI'],
                           prefix + 'MODEL_VERSION': os.environ[prefix + 'MODEL_VERSION']})
        values['LEGACY_MEMORY_UNTIL'] = os.getenv('LEGACY_MEMORY_UNTIL', '')
    keys = ('KUBE_API_URL', 'KUBE_CA_PEM', 'KUBE_ALLOWED_NAMESPACE', 'KUBE_ALLOWED_WORKLOAD')
    if values['AGENT_DIAGNOSTICS_BACKEND'] == 'kubernetes':
        values.update({key: os.environ[key] for key in keys})
        if not all(values[key] for key in keys):
            raise ValueError('Live diagnostics needs the explicit repository-level target and CA inputs')
        endpoint = urlsplit(values['KUBE_API_URL'])
        if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username is not None or endpoint.query or endpoint.fragment:
            raise ValueError('Kubernetes endpoint must use validated HTTPS')
        ssl.create_default_context(cadata=values['KUBE_CA_PEM'])
    elif values['AGENT_DIAGNOSTICS_BACKEND'] == 'provided':
        values.update({key: '' for key in keys})
    else:
        raise ValueError('Diagnostics backend must be provided or kubernetes')
    # Native --file keeps credentials out of process arguments. The temporary file
    # is owner-only and removed even on failure; azd's ignored environment is local.
    with tempfile.NamedTemporaryFile(mode='w', suffix='.env') as variables:
        variables.write(''.join(f'{key}={json.dumps(value)}\n' for key, value in values.items()))
        variables.flush()
        subprocess.run(['azd', 'env', 'set', '--file', variables.name,
                        '--environment', environment, '--no-prompt'], check=True, timeout=30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    configure(parser.parse_args().config)
