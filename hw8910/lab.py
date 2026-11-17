"""Reproducible local release lab; synthetic fixtures are always STUB ONLY.

The same durable preparation/publication primitives are used by flow.py. The
stdlib CLI remains compatible with the original prototype for regression tests.
"""
import argparse
import json
import time
import uuid
from pathlib import Path

import release
from release import canonical, digest


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    release.atomic_write(Path(path), canonical(value) + b'\n')


def initialize(root):
    root = Path(root)
    if root.exists():
        raise FileExistsError('Рабочий каталог уже существует. Выберите новый --workspace.')
    root.mkdir(parents=True)
    runtime = {'base_revision': 'fixture-only', 'tokenizer_sha256': 'a' * 64,
               'template_sha256': 'b' * 64,
               'generation': {'max_new_tokens': 32, 'do_sample': False}, 'dtype': 'float32'}
    for version in ('A', 'B'):
        artifact = root / f'model-{version}.txt'
        artifact.write_text(f'STUB ONLY — version {version}\n', encoding='utf-8')
        files = {artifact.name: digest(artifact.read_bytes())}
        manifest = {'schema_version': 1, 'model_id': f'fixture-{version}',
                    'artifact_uri': artifact.name, 'model_sha256': digest(canonical(files)),
                    'protocol_sha256': 'd' * 64, 'golden_sha256': 'e' * 64,
                    'runtime_sha256': digest(canonical(runtime)), 'runtime': runtime,
                    'file_hashes': files}
        report = {key: manifest[key] for key in release.BINDINGS}
        report.update(status='complete', passed=True, evaluation_run_id=f'fixture-eval-{version}',
                      metrics={'accuracy': .8, 'format_rate': 1.0}, synthetic=True)
        write(root / f'manifest-{version}.json', manifest)
        write(root / f'report-{version}.json', report)
    write(root / 'accepted.json', read(root / 'manifest-A.json'))
    report = read(root / 'report-B.json')
    report.update(status='running', passed='false')
    write(root / 'report-incomplete.json', report)
    report = read(root / 'report-B.json')
    report['passed'] = False
    write(root / 'report-reject.json', report)
    write(root / 'config.json', {'tokenizer_revision': 'r1', 'template_sha256': 'b' * 64,
                                'max_length': 128, 'code_revision': 'prepare-v1'})
    (root / 'input.txt').write_text('Первый пример\nВторой пример\n', encoding='utf-8')
    print('Создан синтетический стенд STUB ONLY:', root, flush=True)


def preparation_key(root):
    root = Path(root)
    config = read(root / 'config.json')
    source = root / config.get('input', 'input.txt')
    return release.cache_key(source, config)


def prepare(root):
    """Validate and normalize real input records, with a durable content cache."""
    root = Path(root)
    config = read(root / 'config.json')
    for name in ('tokenizer_revision', 'template_sha256', 'max_length', 'code_revision'):
        if name not in config:
            raise ValueError(f'Missing preparation setting: {name}')
    if type(config['max_length']) is not int or config['max_length'] < 1:
        raise ValueError('max_length must be a positive integer')
    source = root / config.get('input', 'input.txt')
    payload = source.read_bytes()
    key = release.cache_key(source, config)
    cache = root / 'cache'
    cache.mkdir(exist_ok=True)
    entry, prepared = cache / (key + '.json'), cache / (key + '.jsonl')
    if entry.exists():
        result = read(entry)
        if (result.get('cache_key') == key and result.get('input_sha256') == digest(payload)
                and result.get('config') == config and prepared.is_file()
                and digest(prepared.read_bytes()) == result.get('prepared_sha256')):
            print('CACHE_HIT', result['input_sha256'], 'key=' + key, flush=True)
            return result
    rows = []
    for number, line in enumerate(payload.decode('utf-8').splitlines(), 1):
        if not line.strip():
            continue
        if config.get('input_format') == 'jsonl':
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f'Input row {number} must be an object')
            text = row.get('input', row.get('text'))
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f'Input row {number} requires nonempty input/text')
            row = {**row, 'input': text.strip()}
        else:
            row = {'text': ' '.join(line.split())}
        rows.append(row)
    if not rows:
        raise ValueError('Input must contain at least one nonempty example')
    normalized = b''.join(canonical(row) + b'\n' for row in rows)
    release.atomic_write(prepared, normalized)
    result = {'cache_key': key, 'input_sha256': digest(payload), 'rows': len(rows),
              'config': config, 'prepared_path': str(prepared.resolve()),
              'prepared_sha256': digest(normalized)}
    write(entry, result)
    print('PREPARED', result['input_sha256'], 'key=' + key, flush=True)
    return result


def _journal(root, name):
    path = Path(root) / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def append_record(root, name, record):
    records = _journal(root, name)
    records.append(record)
    release.atomic_write(Path(root) / name, b''.join(canonical(row) + b'\n' for row in records))


def _record_publication(root, event):
    records = _journal(root, 'publications.jsonl')
    ids = [row['release_id'] for row in records]
    if len(ids) != len(set(ids)):
        raise ValueError('Publication journal contains duplicate release IDs')
    if event['release_id'] in ids:
        return False
    append_record(root, 'publications.jsonl', event)
    return True


def recover_publication(root):
    """An intent is committed iff accepted.json contains its exact manifest."""
    root = Path(root)
    pending = root / '.pending-release.json'
    if not pending.exists():
        return
    intent = read(pending)
    pointer = root / 'accepted.json'
    if pointer.exists() and read(pointer) == intent['manifest']:
        _record_publication(root, intent['event'])
        print('RECOVERED_PUBLICATION', intent['event']['release_id'], flush=True)
    pending.unlink()


def publish_once(root, manifest, report, prepared=None, fail_after_publish=False):
    root = Path(root)
    release.validate_release(manifest, report)
    release.verify_artifact(manifest, root)
    synthetic = (report.get('synthetic') is True
                 and manifest.get('model_id', '').startswith('fixture-')
                 and manifest.get('runtime', {}).get('base_revision') == 'fixture-only')
    if not synthetic:
        # This final check also protects the compatible stdlib CLI from bypassing
        # the full gate when a real manifest is supplied outside the Prefect flow.
        from gate_verify import verify_report
        verify_report(manifest, report, root)
    recover_publication(root)
    release_id = digest(canonical({'manifest': manifest, 'report': report}))
    event = {'release_id': release_id, 'model_id': manifest['model_id'],
             'runtime_sha256': manifest['runtime_sha256'],
             'evaluation_run_id': report['evaluation_run_id'],
             'manifest_sha256': digest(canonical(manifest)), 'report_sha256': digest(canonical(report))}
    if prepared:
        event['prepared'] = prepared['input_sha256']
    pointer = root / 'accepted.json'
    previous = pointer.read_bytes() if pointer.exists() else None
    already_current = pointer.exists() and read(pointer) == manifest
    already_recorded = any(row['release_id'] == release_id for row in _journal(root, 'publications.jsonl'))
    if already_current and already_recorded:
        print('ALREADY_PUBLISHED', manifest['model_id'], release_id, flush=True)
        return {**event, 'published': False}
    pending = root / '.pending-release.json'
    write(pending, {'manifest': manifest, 'event': event})
    try:
        release.publish(manifest, report, pointer)
    except BaseException:
        if (pointer.read_bytes() if pointer.exists() else None) == previous:
            pending.unlink(missing_ok=True)
        raise
    if fail_after_publish:
        raise RuntimeError('Инъекция сбоя после указателя до журнала')
    published = _record_publication(root, event)
    pending.unlink(missing_ok=True)
    print('PUBLISHED' if published else 'ALREADY_PUBLISHED', manifest['model_id'], release_id, flush=True)
    return {**event, 'published': published}


def start_attempt(root):
    root = Path(root)
    pointer = root / 'accepted.json'
    return {'attempt_id': str(uuid.uuid4()), 'started_unix': time.time(),
            'accepted_before_sha256': digest(pointer.read_bytes()) if pointer.exists() else None}


def finish_attempt(root, attempt, status, **details):
    root = Path(root)
    pointer = root / 'accepted.json'
    append_record(root, 'attempts.jsonl', {**attempt, **details, 'status': status,
                  'finished_unix': time.time(),
                  'accepted_after_sha256': digest(pointer.read_bytes()) if pointer.exists() else None})


def run(root, report_name='report-B.json', fail_after_prepare=False,
        manifest_name='manifest-B.json', fail_after_publish=False):
    root = Path(root)
    attempt = start_attempt(root)
    try:
        prepared = prepare(root)
        if fail_after_prepare:
            raise RuntimeError('Инъекция сбоя после подготовки')
        manifest = read(root / manifest_name)
        report = read(root / report_name)
        result = publish_once(root, manifest, report, prepared, fail_after_publish)
    except BaseException as exc:
        finish_attempt(root, attempt, 'failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finish_attempt(root, attempt, 'complete', release_id=result['release_id'], published=result['published'])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.lab'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    run_parser = sub.add_parser('run')
    run_parser.add_argument('--report', default='report-B.json')
    run_parser.add_argument('--manifest', default='manifest-B.json')
    run_parser.add_argument('--fail-after-prepare', action='store_true')
    run_parser.add_argument('--fail-after-publish', action='store_true')
    args = parser.parse_args()
    if args.command == 'init':
        initialize(args.workspace)
    else:
        run(args.workspace, args.report, args.fail_after_prepare, args.manifest, args.fail_after_publish)


if __name__ == '__main__':
    main()
