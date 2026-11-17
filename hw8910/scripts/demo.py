"""Small make helpers for inspecting releases and demonstrating localhost HTTP."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import release


def input_change(workspace):
    workspace = Path(workspace)
    manifest = json.loads((workspace / 'manifest-B.json').read_text())
    if manifest.get('runtime', {}).get('base_revision') != 'fixture-only':
        raise ValueError('input-change is only available for an initialized STUB workspace')
    source = workspace / 'input.txt'
    before = release.file_digest(source)
    with source.open('a', encoding='utf-8') as stream:
        stream.write('\nДополнительный пример для проверки инвалидации кеша\n')
    print('INPUT_CHANGED', source)
    print('before_sha256:', before)
    print('after_sha256: ', release.file_digest(source))


def publications(workspace):
    path = Path(workspace) / 'publications.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    print('publication_count:', len(rows))
    for row in rows:
        print(row['model_id'], row['release_id'])


def accepted_hash(workspace):
    path = Path(workspace) / 'accepted.json'
    print(release.file_digest(path), path)


def http(url, action):
    body = None
    route = action
    if action in ('generate', 'invalid-request'):
        route = 'generate'
        body = json.dumps({
            'prompt': 'In fd, disable ignore-file filtering without enabling hidden entries. '
                      'Which flag? A. --follow B. --no-ignore C. --hidden',
            'max_new_tokens': 0 if action == 'invalid-request' else 32,
        }).encode()
    request = Request(url.rstrip('/') + '/' + route, data=body,
                      headers={'Content-Type': 'application/json'})
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(request, timeout=120)
    except HTTPError as error:
        response = error
    with response:
        print('HTTP', response.status)
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))
    # This command inspects HTTP responses; 503 during warmup and 422 for invalid
    # input are intentional observations, not successful model generations.


def check_original(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    archive = subprocess.run(
        ['git', '-C', str(ROOT.parent), 'archive', 'd6c70a7', 'hw8910'],
        check=True, stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(output, filter='data')
    environment = {**os.environ, 'HW8910_TARGET_ROOT': str(output / 'hw8910')}
    return subprocess.run(
        [sys.executable, '-m', 'unittest', 'discover', '-s', 'integration',
         '-p', 'test_integration.py', '-v'], cwd=ROOT, env=environment).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('input-change', 'publications', 'accepted-hash'):
        commands.add_parser(name).add_argument('--workspace', type=Path, required=True)
    request = commands.add_parser('http')
    request.add_argument('--url', required=True)
    request.add_argument('--action', choices=['health', 'ready', 'debug', 'generate', 'invalid-request'], required=True)
    commands.add_parser('check-original').add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'http':
            http(args.url, args.action)
        elif args.command == 'check-original':
            return check_original(args.output)
        else:
            {'input-change': input_change, 'publications': publications,
             'accepted-hash': accepted_hash}[args.command](args.workspace)
    except (OSError, ValueError, URLError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
