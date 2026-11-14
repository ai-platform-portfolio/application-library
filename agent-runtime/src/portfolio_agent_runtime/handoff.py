"""Small CLI bindings for isolated fetch, snapshot, validation and publication jobs."""
import argparse
import json
import os
from pathlib import Path
import re

import yaml

from . import coding


def prepare(config_path, investigation_path, output, incident_id):
    configuration = yaml.safe_load(Path(config_path).read_text())
    result = coding.read_json(Path(investigation_path))
    if (result.get('diagnosis', {}).get('decision') != 'actionable'
            or result.get('evidence_source') != 'live_kubernetes'
            or not {'workload_state', 'recent_events'} <= set(result.get('tool_calls', []))
            or not re.fullmatch(r'[a-f0-9]{32}', result.get('trace_id') or '')):
        raise ValueError('Coding requires an actionable, traced live investigation')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    policy = {key: configuration[key] for key in ('repository', 'base_ref', 'editable_paths', 'checks', 'instructions')}
    with coding.github(os.getenv('GITHUB_READ_TOKEN', '')) as client:
        current = coding.checked(client, 'GET', f'/repos/{policy["repository"]}/git/ref/heads/{policy["base_ref"]}')
    policy.update(base_sha=current['object']['sha'], incident_id=incident_id)
    coding.write_json(output / 'investigation.json', result)
    coding.write_json(output / 'policy.json', policy)
    policy = coding.load_policy(output / 'policy.json')
    coding.fetch(policy, output / 'source', os.getenv('GITHUB_READ_TOKEN', ''))
    files = {}
    for path in sorted((output / 'source').rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(output / 'source').as_posix()
        if not any(relative.startswith(prefix) for prefix in configuration['context_paths']):
            continue
        if path.stat().st_size > 16384:
            raise ValueError('Declared context contains an oversized file; narrow its source path')
        files[relative] = path.read_text()
    request = {'operation': 'propose', 'incident_id': incident_id, 'base_sha': policy['base_sha'],
               'diagnosis': {'answer': result['diagnosis']['answer'], 'trace_id': result['trace_id']}, 'files': files}
    if not files or len(files) > 32 or len(json.dumps(files).encode()) > 98304:
        raise ValueError('Declared context exceeds the bounded source snapshot')
    coding.write_json(output / 'request.json', request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'validate', 'publish'])
    parser.add_argument('directory', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--investigation', type=Path)
    parser.add_argument('--incident-id')
    args = parser.parse_args()
    if args.operation == 'prepare':
        prepare(args.config, args.investigation, args.directory, args.incident_id)
        return
    policy = coding.load_policy(args.directory / 'policy.json')
    proposal = coding.read_json(args.directory / 'proposal.json')
    if args.operation == 'validate':
        report = coding.validate(policy, args.directory / 'source', proposal,
                                 sandbox_image=os.environ['VALIDATION_IMAGE'])
        coding.write_json(args.directory / 'validation.json', report)
        if not report['passed']:
            raise SystemExit('Proposal failed its declared validation')
    else:
        report = coding.read_json(args.directory / 'validation.json')
        investigation = coding.read_json(args.directory / 'investigation.json')
        if report.get('sandbox_image') != investigation['deployment']['image']:
            raise ValueError('Validation did not use the declared immutable sandbox')
        result = coding.publish(policy, args.directory / 'source', proposal, report, os.environ['GITHUB_WRITE_TOKEN'])
        coding.write_json(args.directory / 'publication.json', result)
        print(json.dumps(result))


if __name__ == '__main__':
    main()
