"""Recompute the unchanged hw67 gate from pinned full reports and raw answers.

Source hashes detect post-evaluation edits; score is recomputed so a hand-written
passed flag or metric does not constitute model evaluation. Trusted gate code is
loaded from the adjacent hw67 project, never executed from a manifest URI.
"""
import importlib.util
import json
from pathlib import Path

import release

REQUIRED_SOURCES = {'baseline_eval', 'candidate_eval', 'baseline_outputs', 'candidate_outputs',
                    'golden', 'behavioral', 'training_index', 'params', 'evaluation_code'}


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def verify_report(manifest: dict, report: dict, workspace: Path) -> dict:
    """Return the recomputed decision, including a genuine quality rejection."""
    root = Path(workspace)
    sources = report.get('sources')
    if not isinstance(sources, dict) or not REQUIRED_SOURCES <= sources.keys():
        raise ValueError('Real release requires full evaluation evidence sources')
    paths = {}
    for name, descriptor in sources.items():
        if not isinstance(descriptor, dict) or not {'path', 'sha256'} <= descriptor.keys():
            raise ValueError(f'Invalid evaluation source: {name}')
        if not isinstance(descriptor['path'], str) or not descriptor['path']:
            raise ValueError(f'Invalid source path: {name}')
        expected = descriptor['sha256']
        if not isinstance(expected, str) or release.SHA256.fullmatch(expected) is None:
            raise ValueError(f'Invalid source digest: {name}')
        path = Path(descriptor['path'])
        if not path.is_absolute():
            path = root / path
        if release.file_digest(path) != expected:
            raise ValueError(f'Evaluation source changed: {name}')
        paths[name] = path
    trusted = Path(__file__).resolve().parents[1] / 'hw67' / 'evaluation'
    if release.file_digest(paths['evaluation_code']) != release.file_digest(trusted / 'solution.py'):
        raise ValueError('Evaluation source does not match the trusted hw67 quality gate')
    scorer = _load_module('hw8910_trusted_hw67_solution', trusted / 'solution.py')
    contract = _load_module('hw8910_trusted_hw67_contract', trusted / 'contract.py')
    config = json.loads(paths['params'].read_text(encoding='utf-8'))
    golden, behavioral, training = [_rows(paths[name]) for name in ('golden', 'behavioral', 'training_index')]
    proof = contract.validate_golden(golden, training, behavioral)
    protocol = {'config': config, 'golden': golden, 'behavioral': behavioral,
                'training_index': training,
                'evaluation_code': {path.name: path.read_text(encoding='utf-8')
                                    for path in sorted(trusted.glob('*.py'))}}
    protocol_sha256 = contract.fingerprint(protocol)
    golden_sha256 = contract.fingerprint(golden)
    baseline, candidate = [json.loads(paths[role + '_eval'].read_text(encoding='utf-8'))
                           for role in ('baseline', 'candidate')]
    trusted_baseline = json.loads((trusted / 'baseline.json').read_text(encoding='utf-8'))
    if baseline.get('model') != trusted_baseline:
        raise ValueError('Evaluation baseline differs from the accepted hw67 baseline')
    if manifest.get('model_spec') != candidate.get('model'):
        raise ValueError('Manifest model_spec differs from evaluated candidate')
    if manifest.get('model_sha256') != candidate.get('model_sha256'):
        raise ValueError('Candidate evaluated weight digest differs from manifest')
    if manifest.get('model_id') != candidate.get('model', {}).get('run_id'):
        raise ValueError('Manifest model_id differs from candidate training run')
    if report.get('evaluation_run_id') != candidate.get('evaluation_run_id'):
        raise ValueError('Release/evaluation run ID mismatch')
    if report.get('metrics') != {name: candidate.get('overall', {}).get(name)
                                 for name in ('accuracy', 'format_rate')}:
        raise ValueError('Release metrics differ from full candidate evaluation')
    for name, expected in (('protocol_sha256', protocol_sha256), ('golden_sha256', golden_sha256)):
        if manifest.get(name) != expected or report.get(name) != expected:
            raise ValueError('Release does not bind current evaluation ' + name)
    baseline_spec_sha256 = contract.fingerprint(baseline.get('model'))
    expected_ids = {row['id'] for row in golden + behavioral}
    candidate_runtime = candidate.get('runtime')
    baseline_runtime = baseline.get('runtime')
    if (not isinstance(candidate_runtime, dict) or not candidate_runtime
            or not isinstance(baseline_runtime, dict) or not baseline_runtime
            or candidate_runtime != manifest.get('runtime')):
        raise ValueError('Candidate evaluation/runtime manifest mismatch')
    for name in ('base_revision', 'template_sha256', 'generation', 'dtype', 'device'):
        if baseline_runtime.get(name) != candidate_runtime.get(name):
            raise ValueError('Baseline/candidate runtime differs: ' + name)
    for role, evaluation in (('baseline', baseline), ('candidate', candidate)):
        runtime = evaluation.get('runtime')
        if not isinstance(runtime, dict) or not runtime:
            raise ValueError(f'Missing {role} runtime')
        runtime_sha256 = release.digest(release.canonical(runtime))
        if (evaluation.get('status') != 'complete'
                or evaluation.get('protocol_sha256') != protocol_sha256
                or evaluation.get('golden_sha256') != golden_sha256
                or evaluation.get('baseline_spec_sha256') != baseline_spec_sha256
                or evaluation.get('runtime_sha256') != runtime_sha256
                or evaluation.get('evaluation_run_id') != report.get('evaluation_run_id')):
            raise ValueError(f'Incompatible or incomplete {role} evaluation provenance')
        if role == 'candidate' and runtime_sha256 != manifest.get('runtime_sha256'):
            raise ValueError('Candidate runtime digest differs from release')
        outputs = json.loads(paths[role + '_outputs'].read_text(encoding='utf-8'))
        if not isinstance(outputs, list):
            raise ValueError('Raw outputs must be a list')
        try:
            by_id = {output['id']: output for output in outputs}
        except (KeyError, TypeError) as exc:
            raise ValueError('Invalid raw output record') from exc
        if len(by_id) != len(outputs) or set(by_id) != expected_ids:
            raise ValueError(f'Invalid {role} output IDs')
        if evaluation.get('outputs_sha256') != contract.fingerprint(outputs):
            raise ValueError(f'{role} outputs do not match evaluated digest')
        calculated = scorer.score(golden, [by_id[row['id']] for row in golden])
        if any(evaluation.get(key) != calculated[key] for key in ('overall', 'slices', 'per_item')):
            raise ValueError(f'{role} metrics do not match raw answers')
        behavior = scorer.score(behavioral, [by_id[row['id']] for row in behavioral])
        if evaluation.get('behavioral') != behavior:
            raise ValueError(f'{role} behavioral metrics do not match raw answers')
        if evaluation.get('leakage') != proof:
            raise ValueError(f'{role} leakage proof differs from current data')
    try:
        decision = scorer.quality_gate(baseline, candidate, config['critical_slices'])
    except ValueError as exc:
        decision = {'passed': False, 'reason': str(exc)}
    if type(report.get('passed')) is not bool or report['passed'] is not decision['passed']:
        raise ValueError('Release passed flag differs from recomputed hw67 gate')
    return decision
