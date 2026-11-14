"""Independent datasets and six bounded contract checks; no model calls or persistence."""
from pathlib import Path
import re
import yaml


def load_case(path):
    case = yaml.safe_load(Path(path).read_text())
    if not isinstance(case, dict) or case.get('schema_version') != 1:
        raise ValueError('Unsupported evaluation case')
    if not isinstance(case.get('reference'), dict) or not isinstance(case.get('rubric'), dict):
        raise ValueError('Case requires an independent reference and rubric')
    if not isinstance(case.get('id'), str) or type(case.get('version')) is not int or case['version'] < 1:
        raise ValueError('Case requires an id and positive integer version')
    payload = {'incident': case['incident'], 'evidence': case['tool_fixtures']}
    return case, payload


def score(case, result, config):
    # These are contract checks. A correct root cause still needs human assessment.
    automated = case['rubric'].get('automated', {})
    checks = {
        'required_tools': set(automated.get('required_tools', [])) <= set(result['tool_calls']),
        'forbidden_tools': not (set(automated.get('forbidden_tools', [])) & set(result['tool_calls'])),
        'known_tools': set(result['tool_calls']) <= set(config['tools']),
        'tool_budget': len(result['tool_calls']) <= config['limits']['tool_calls'],
        'request_budget': result['model_requests'] <= config['limits']['model_requests'],
        'no_forbidden_claims': not any(re.search(pattern, result['diagnosis']['answer'])
                                       for pattern in automated.get('forbidden_answer_patterns', [])),
    }
    return {'case': case['id'], 'case_version': case['version'], 'checks': checks,
            'status': 'human_review_required' if all(checks.values()) else 'failed',
            'human_rubric': case['rubric'].get('checks', case['rubric'].get('criteria', [])),
            'hard_failures': case['rubric'].get('hard_failures', []), 'result': result}
