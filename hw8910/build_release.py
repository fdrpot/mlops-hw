"""Reuse HW67 training, evaluate the exact HTTP runtime, retain baseline on rejection.

No training or threshold adjustment takes place. All release reports are generated
from raw outputs through the unchanged HW67 scorer and gate.
"""
import argparse
import gc
import importlib.util
import json
import os
from pathlib import Path
import shutil
import time

import release
from lab import read, write

PROJECT = Path(__file__).resolve().parent
HW67 = PROJECT.parent / 'hw67'
REVISION = '70d244cc86ccca08cf5af4e1e306ecf908b1ad5e'
TOKEN_FILES = ('tokenizer.json', 'tokenizer_config.json', 'merges.txt', 'vocab.json')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fingerprint(value):
    # HW67 protocol deliberately retains its original JSON serialization.
    import hashlib
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def prepare_models(tracking_uri):
    import mlflow
    from huggingface_hub import snapshot_download
    models = PROJECT / 'models'
    models.mkdir(exist_ok=True)
    snapshot = Path(snapshot_download(
        'Qwen/Qwen3-1.7B', revision=REVISION, local_files_only=True,
        allow_patterns=['*.json', '*.safetensors', '*.txt', '*.jinja']))
    base = models / 'base'
    if not base.exists():
        base.symlink_to(snapshot, target_is_directory=True)
    if base.resolve() != snapshot.resolve():
        raise ValueError('Existing models/base does not point to the pinned revision')
    candidate = read(HW67 / 'metrics/candidate-model.json')
    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.MlflowClient()
    version = client.get_model_version(candidate['name'], candidate['version'])
    run = client.get_run(candidate['run_id'])
    if version.run_id != candidate['run_id'] or run.info.status != 'FINISHED':
        raise ValueError('Registry version and completed training run differ')
    origin = PROJECT / '.download'
    downloaded = Path(mlflow.artifacts.download_artifacts(
        run_id=candidate['run_id'], artifact_path='model', dst_path=str(origin)))
    adapter = downloaded / 'artifacts/adapter'
    training = read(adapter / 'manifest.json')
    if training['model']['revision'] != REVISION:
        raise ValueError('Base revision differs from training')
    shutil.copytree(adapter, models / 'adapter', dirs_exist_ok=True)
    return models, candidate, {
        'training_run_id': run.info.run_id, 'training_status': run.info.status,
        'artifact_uri': run.info.artifact_uri, 'registry_version': candidate,
        'metrics': run.data.metrics, 'tags': run.data.tags,
        'training_manifest': training,
    }


def runtime_for(token_root, params, device):
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(token_root, local_files_only=True)
    token_hashes = {n: release.file_digest(token_root / n) for n in TOKEN_FILES if (token_root / n).exists()}
    return {'base_revision': REVISION,
            'tokenizer_sha256': release.digest(release.canonical(token_hashes)),
            'template_sha256': release.digest(tokenizer.chat_template.encode()),
            'generation': {'system': params['system'], 'do_sample': False,
                           'seed': params['seed'], 'max_new_tokens': params['max_new_tokens'],
                           'enable_thinking': params['enable_thinking']},
            'dtype': 'bfloat16', 'device': device, 'max_input_tokens': 2048,
            'implementation_sha256': release.file_digest(PROJECT / 'real_backend.py')}


def build_manifest(models, role, spec, runtime, protocol_hash, golden_hash):
    directories = ('base', 'adapter') if role == 'candidate' else ('base',)
    hashes = {f'{directory}/{path.name}': release.file_digest(path)
              for directory in directories for path in sorted((models / directory).iterdir())
              if path.is_file()}
    return {'schema_version': 1,
            'model_id': spec['run_id'] if role == 'candidate' else f'baseline-Qwen3-1.7B-{REVISION}',
            'artifact_uri': str(models), 'file_hashes': hashes,
            'model_sha256': release.digest(release.canonical(hashes)),
            'runtime': runtime, 'runtime_sha256': release.digest(release.canonical(runtime)),
            'protocol_sha256': protocol_hash, 'golden_sha256': golden_hash,
            'model_spec': spec, 'role': role}


def build(workspace, tracking_uri, device='auto'):
    from real_backend import RealBackend
    import mlflow
    import torch
    if device == 'auto':
        device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    workspace = Path(workspace).resolve()
    workspace.mkdir(exist_ok=True, parents=True)
    evidence = PROJECT / 'evidence/real'
    sources = evidence / 'source'
    sources.mkdir(exist_ok=True, parents=True)
    scorer = module('hw67_solution', HW67 / 'evaluation/solution.py')
    contract = module('hw67_contract', HW67 / 'evaluation/contract.py')
    params = read(HW67 / 'evaluation/params.json')
    rows = contract.read_rows(HW67 / params['golden'])
    behavioral = contract.read_rows(HW67 / params['behavioral'])
    training = contract.read_rows(HW67 / params['training_index'])
    proof = contract.validate_golden(rows, training, behavioral)
    base_spec = read(HW67 / 'evaluation/baseline.json')
    if base_spec != {'kind': 'base', 'name': 'Qwen/Qwen3-1.7B', 'revision': REVISION}:
        raise ValueError('The retained baseline differs from the pinned base')
    protocol = {'config': params, 'golden': rows, 'behavioral': behavioral,
                'training_index': training,
                'evaluation_code': {p.name: p.read_text() for p in sorted((HW67 / 'evaluation').glob('*.py'))}}
    protocol_hash = fingerprint(protocol)
    models, candidate_spec, origin = prepare_models(tracking_uri)
    write(evidence / 'training-origin.json', origin)
    for key, path in [('golden', HW67 / params['golden']),
                      ('behavioral', HW67 / params['behavioral']),
                      ('training_index', HW67 / params['training_index']),
                      ('params', HW67 / 'evaluation/params.json'),
                      ('evaluation_code', HW67 / 'evaluation/solution.py')]:
        shutil.copyfile(path, sources / path.name)
    manifests = {}
    for role, spec in [('baseline', base_spec), ('candidate', candidate_spec)]:
        token_root = models / ('adapter' if role == 'candidate' else 'base')
        runtime = runtime_for(token_root, params, device)
        manifests[role] = build_manifest(models, role, spec, runtime, protocol_hash, fingerprint(rows))
        write(workspace / f'manifest-{role}.json', manifests[role])
        write(evidence / f'manifest-{role}.json', manifests[role])
    # This initializes the pre-existing baseline; it does NOT grant it a new PASS.
    pointer = workspace / 'accepted.json'
    if not pointer.exists():
        retained = {**manifests['baseline'], 'acceptance': {
            'kind': 'retained_baseline',
            'source': str(HW67 / 'evaluation/baseline.json'),
            'source_sha256': release.file_digest(HW67 / 'evaluation/baseline.json'),
            'note': 'Existing baseline retained; no claim of a new behavioral PASS'}}
        write(pointer, retained)
    write(workspace / 'config.json', {
        'tokenizer_revision': REVISION,
        'template_sha256': manifests['candidate']['runtime']['template_sha256'],
        'tokenizer_sha256': manifests['candidate']['runtime']['tokenizer_sha256'],
        'max_length': 2048, 'code_revision': release.file_digest(PROJECT / 'lab.py'),
        'input': 'input.jsonl', 'input_format': 'jsonl'})
    shutil.copyfile(HW67 / params['golden'], workspace / 'input.jsonl')
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment('hw8910-evaluation')
    reports = {}
    with mlflow.start_run(run_name='identical-eval-and-http-runtime') as run:
        for role in ('baseline', 'candidate'):
            manifest = manifests[role]
            print('LOAD', role, manifest['model_id'], flush=True)
            backend = RealBackend(manifest, workspace)
            outputs = []
            for index, row in enumerate(rows + behavioral, 1):
                started = time.perf_counter()
                try:
                    response = backend.generate(row['input'], params['max_new_tokens'])
                    answer, error, count = response['answer'], None, response['output_tokens']
                except Exception as exc:
                    answer, error, count = '', f'{type(exc).__name__}: {exc}', 0
                outputs.append({'id': row['id'], 'raw': answer, 'error': error,
                                'output_tokens': count, 'latency_seconds': time.perf_counter() - started})
                print(role, index, '/', len(rows + behavioral), 'tokens', count, 'error', error, flush=True)
            write(sources / f'{role}-outputs.json', outputs)
            by_id = {o['id']: o for o in outputs}
            report = scorer.score(rows, [by_id[r['id']] for r in rows])
            report.update(status='complete', model=manifest['model_spec'],
                          model_sha256=manifest['model_sha256'],
                          protocol_sha256=protocol_hash, golden_sha256=fingerprint(rows),
                          baseline_spec_sha256=fingerprint(base_spec),
                          runtime=manifest['runtime'], runtime_sha256=manifest['runtime_sha256'],
                          behavioral=scorer.score(behavioral, [by_id[r['id']] for r in behavioral]),
                          leakage=proof, outputs_sha256=fingerprint(outputs),
                          evaluation_run_id=run.info.run_id)
            write(sources / f'{role}-eval.json', report)
            reports[role] = report
            mlflow.log_metrics({f'{role}.{key}': report['overall'][key] for key in ('accuracy', 'format_rate')})
            del backend
            gc.collect()
            if device == 'mps':
                torch.mps.empty_cache()
        try:
            decision = scorer.quality_gate(reports['baseline'], reports['candidate'], params['critical_slices'])
        except ValueError as exc:
            decision = {'passed': False, 'reason': str(exc)}
        manifest = manifests['candidate']
        mapping = {
            'baseline_eval': sources / 'baseline-eval.json',
            'candidate_eval': sources / 'candidate-eval.json',
            'baseline_outputs': sources / 'baseline-outputs.json',
            'candidate_outputs': sources / 'candidate-outputs.json',
            'golden': sources / Path(params['golden']).name,
            'behavioral': sources / Path(params['behavioral']).name,
            'training_index': sources / Path(params['training_index']).name,
            'params': sources / 'params.json', 'evaluation_code': sources / 'solution.py'}
        wrapper = {key: manifest[key] for key in release.BINDINGS}
        wrapper.update(status='complete', passed=decision['passed'], decision=decision,
                       evaluation_run_id=run.info.run_id,
                       metrics={key: reports['candidate']['overall'][key] for key in ('accuracy', 'format_rate')},
                       gate_verified=True, synthetic=False,
                       sources={key: {'path': str(path), 'sha256': release.file_digest(path)}
                                for key, path in mapping.items()})
        write(workspace / 'release-report.json', wrapper)
        write(evidence / 'release-report.json', wrapper)
        write(evidence / 'accepted.json', read(pointer))
        mlflow.set_tags({'training_run_id': candidate_spec['run_id'],
                         'runtime_sha256': manifest['runtime_sha256'],
                         'gate_passed': str(decision['passed']), 'kind': 'hw8910-real-evaluation'})
        mlflow.log_artifacts(str(evidence), 'evidence')
        print('EVALUATION_RUN', run.info.run_id, 'DECISION', decision, flush=True)
    return wrapper


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.real'))
    parser.add_argument('--tracking-uri', default='http://127.0.0.1:5006')
    parser.add_argument('--device', choices=['auto', 'cpu', 'mps'], default='auto')
    args = parser.parse_args()
    build(args.workspace, args.tracking_uri, args.device)


if __name__ == '__main__':
    main()
