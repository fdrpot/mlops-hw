"""Run the real rejection, HTTP smoke/restart, validation and 8+8 benchmark."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import benchmark
import release

HTTP = build_opener(ProxyHandler({}))


def request(base, path, payload=None):
    message = Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
    try:
        with HTTP.open(message, timeout=120) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=ROOT / '.real')
    parser.add_argument('--benchmark-output', type=Path, default=ROOT / 'evidence/benchmark')
    args = parser.parse_args()
    root = args.workspace.resolve()
    evidence = ROOT / 'evidence'
    pointer = root / 'accepted.json'
    before = release.file_digest(pointer)
    command = [sys.executable, 'flow.py', '--workspace', str(root),
               '--manifest', 'manifest-candidate.json', '--report', 'release-report.json']
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=180)
    (evidence / 'real-flow-rejection.log').write_text(result.stdout)
    after = release.file_digest(pointer)
    if result.returncode == 0 or after != before:
        raise AssertionError('Rejected candidate changed accepted baseline')
    trace = {'synthetic': False, 'rejection': {'command': command, 'exit_code': result.returncode,
             'accepted_before_sha256': before, 'accepted_after_sha256': after}, 'http': []}
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    manifest = json.loads(pointer.read_text())
    golden = [json.loads(line) for line in (evidence / 'real/source/golden.jsonl').read_text().splitlines()]
    prompt = golden[0]['input']
    server_log = (evidence / 'real-api.log').open('w')

    def start():
        process = subprocess.Popen([sys.executable, 'service.py', '--workspace', str(root),
                                    '--backend', 'real', '--port', str(port),
                                    '--warmup-seconds', '3', '--debug'], cwd=ROOT,
                                   stdout=server_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError('API exited before health, see real-api.log')
            try:
                status, payload = request(base, '/health')
                trace['http'].append({'path': '/health', 'status': status, 'response': payload})
                return process
            except (URLError, OSError):
                time.sleep(.1)
        process.terminate()
        raise TimeoutError('API health timeout')

    def stop(process):
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    process = start()
    try:
        status, payload = request(base, '/ready')
        trace['http'].append({'path': '/ready', 'phase': 'loading', 'status': status, 'response': payload})
        if status != 503:
            raise AssertionError('Real model was ready before load/warmup')
        status, payload = request(base, '/generate', {'prompt': prompt, 'max_new_tokens': 32})
        trace['http'].append({'path': '/generate', 'phase': 'loading', 'status': status, 'response': payload})
        if status != 503:
            raise AssertionError('Early generation must be unavailable')
        benchmark.wait_ready(base, 180)
        status, response = request(base, '/generate', {'prompt': prompt, 'max_new_tokens': 32})
        if (status != 200 or 'STUB ONLY' in response.get('answer', '')
                or response['model_id'] != manifest['model_id']
                or response['runtime_sha256'] != manifest['runtime_sha256']):
            raise AssertionError('Real smoke identity mismatch')
        trace['smoke'] = {'status': status, 'prompt': prompt, 'response': response}
        trace['invalid_requests'] = []
        for payload in ({'prompt': ''}, {'prompt': prompt, 'max_new_tokens': 0},
                        {'prompt': prompt, 'max_new_tokens': 100000},
                        {'prompt': prompt, 'max_new_tokens': '32'},
                        {'prompt': prompt, 'max_new_tokens': 16}, {'prompt': '猫' * 3000}):
            calls_before = request(base, '/debug')[1]['calls']
            status, error = request(base, '/generate', payload)
            calls_after = request(base, '/debug')[1]['calls']
            trace['invalid_requests'].append({'status': status, 'error': error,
                                             'calls_before': calls_before, 'calls_after': calls_after})
            if not 400 <= status < 500 or calls_after != calls_before:
                raise AssertionError('Invalid real request invoked generation')
        benchmark.PROMPTS = tuple(row['input'] for row in golden[:8])
        summary = benchmark.run(base, args.benchmark_output, 8, 120, 180, False)
        if any(row['errors'] for row in summary):
            raise AssertionError('Real benchmark has errors; all attempts preserved')
        trace['benchmark'] = summary
    finally:
        stop(process)
    process = start()
    try:
        benchmark.wait_ready(base, 180)
        status, response = request(base, '/generate', {'prompt': prompt, 'max_new_tokens': 32})
        trace['restart_smoke'] = {'status': status, 'response': response}
        if status != 200 or response['model_id'] != manifest['model_id']:
            raise AssertionError('Restart did not load accepted baseline')
    finally:
        stop(process)
        server_log.close()
        (evidence / 'real-smoke.json').write_text(json.dumps(trace, ensure_ascii=False, indent=2) + '\n')
    print('PASS real rejection, early503, smoke, invalid requests, 8+8 benchmark, restart')


if __name__ == '__main__':
    main()
