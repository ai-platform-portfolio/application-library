"""Native azd predeploy hook for reviewed knowledge and a stable memory store."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
import yaml

from .knowledge import ensure_memory, publish


def configure(path):
    config = yaml.safe_load(Path(path).read_text())
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
        memory_name = environment + '-memory'
        with DefaultAzureCredential() as credential, AIProjectClient(
                endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'], credential=credential) as project:
            ensure_memory(project, memory_name, config['model'], memory.get('embedding_model', 'embedding'),
                          memory.get('retention_days', 30))
            with project.get_openai_client() as openai:
                manifest = publish(project, openai, directory, environment, revision, repository, source_path)
        # The caller uploads this with its existing release/evaluation artifacts.
        record = Path(os.environ['RUNNER_TEMP']) / 'acceptance' / 'knowledge.json'
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(manifest, indent=2))
    subprocess.run(['azd', 'env', 'set', 'MEMORY_STORE_NAME=' + memory_name,
                    'KNOWLEDGE_MANIFEST_JSON=' + json.dumps(manifest, separators=(',', ':')),
                    '--environment', environment, '--no-prompt'], check=True, timeout=30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    configure(parser.parse_args().config)
