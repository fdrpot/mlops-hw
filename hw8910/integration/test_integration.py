"""Eight regressions using the same original/fixed CLI and HTTP contracts.

HW8910_TARGET_ROOT points at an untouched prototype for the red run. No import
failure or nonexistent command is accepted as evidence of a regression.
"""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

TARGET = Path(os.environ.get('HW8910_TARGET_ROOT', Path(__file__).resolve().parents[1])).resolve()
HTTP = build_opener(ProxyHandler({}))


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ReleaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hw8910-integration-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        result = self.lab('init')
        self.assertEqual(result.returncode, 0, result.stdout)

    def lab(self, *args):
        command = [sys.executable, str(TARGET / 'lab.py'), '--workspace', str(self.root), *args]
        result = subprocess.run(command, cwd=TARGET, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=20)
        print(json.dumps({'kind': 'subprocess', 'test': self._testMethodName,
                          'args': list(args), 'exit_code': result.returncode,
                          'output': result.stdout.strip()}, ensure_ascii=False), flush=True)
        return result

    def test_process_recovery_and_changed_bytes(self):
        failure = self.lab('run', '--fail-after-prepare')
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn('PREPARED', failure.stdout)
        resumed = self.lab('run')
        self.assertEqual(resumed.returncode, 0, resumed.stdout)
        self.assertIn('CACHE_HIT', resumed.stdout)
        self.assertNotIn('PREPARED', resumed.stdout)
        with (self.root / 'input.txt').open('a') as stream:
            stream.write('Изменение содержимого по тому же пути\n')
        changed = self.lab('run')
        self.assertEqual(changed.returncode, 0, changed.stdout)
        self.assertIn('PREPARED', changed.stdout, 'changed bytes must invalidate persisted cache')
        self.assertNotIn('CACHE_HIT', changed.stdout)

    def test_process_every_preparation_config_invalidates(self):
        self.assertEqual(self.lab('run').returncode, 0)
        for key, value in [('tokenizer_revision', 'r2'), ('template_sha256', 'f' * 64),
                           ('max_length', 256), ('code_revision', 'prepare-v2')]:
            with self.subTest(setting=key):
                config = read(self.root / 'config.json')
                config[key] = value
                write(self.root / 'config.json', config)
                result = self.lab('run')
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn('PREPARED', result.stdout, f'{key} must invalidate cache')

    def test_process_incomplete_foreign_and_rejected_reports_preserve_pointer(self):
        pointer = self.root / 'accepted.json'
        before = digest(pointer)
        for report in ['report-incomplete.json', 'report-A.json', 'report-reject.json']:
            with self.subTest(report=report):
                result = self.lab('run', '--report', report)
                self.assertNotEqual(result.returncode, 0, f'{report} must be rejected')
                self.assertEqual(digest(pointer), before)
        print(json.dumps({'kind': 'rejection-proof', 'before_sha256': before,
                          'after_sha256': digest(pointer)}), flush=True)

    def test_process_repeat_publication_has_one_event(self):
        for _ in range(2):
            result = self.lab('run')
            self.assertEqual(result.returncode, 0, result.stdout)
        events = [json.loads(row) for row in (self.root / 'publications.jsonl').read_text().splitlines()]
        self.assertEqual(len(events), 1, 'same successful release in two processes produces one publication')
        self.assertEqual(events[0]['model_id'], 'fixture-B')

    @contextmanager
    def server(self, warmup=0):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        log_path = Path(self.temp.name) / f'server-{time.time_ns()}.log'
        with log_path.open('w+') as log:
            process = subprocess.Popen(
                [sys.executable, str(TARGET / 'service.py'), '--workspace', str(self.root),
                 '--port', str(port), '--warmup-seconds', str(warmup)],
                cwd=TARGET, stdout=log, stderr=subprocess.STDOUT,
            )
            base = f'http://127.0.0.1:{port}'
            try:
                deadline = time.monotonic() + 12
                while True:
                    if process.poll() is not None:
                        self.fail(f'Server exited before HTTP health: {log_path.read_text()}')
                    try:
                        code, payload = self.request(base, '/health')
                        if code == 200:
                            break
                    except (URLError, TimeoutError, ConnectionError):
                        pass
                    if time.monotonic() >= deadline:
                        self.fail(f'No HTTP health: {log_path.read_text()}')
                    time.sleep(.025)
                yield base
            finally:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def request(self, base, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        req = Request(base + path, data=body, headers={'Content-Type': 'application/json'})
        try:
            response = HTTP.open(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def wait_ready(self, base):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            code, body = self.request(base, '/ready')
            if code == 200 and body['ready']:
                return
            time.sleep(.025)
        self.fail('Warmup did not complete')

    def test_http_readiness_blocks_generation_during_warmup(self):
        with self.server(warmup=1.2) as base:
            self.assertEqual(self.request(base, '/health')[0], 200)
            code, ready = self.request(base, '/ready')
            self.assertEqual(code, 503, 'health is alive before model is ready')
            self.assertFalse(ready['ready'])
            before = self.request(base, '/debug')[1]['calls']
            code, _ = self.request(base, '/generate', {'prompt': 'test', 'max_new_tokens': 32})
            self.assertEqual(code, 503)
            self.assertEqual(self.request(base, '/debug')[1]['calls'], before)
            self.wait_ready(base)
            code, payload = self.request(base, '/generate', {'prompt': 'test', 'max_new_tokens': 32})
            self.assertEqual(code, 200)
            self.assertEqual(payload['model_id'], 'fixture-A')
            self.assertTrue(self.request(base, '/debug')[1]['warmup_finished'])

    def test_http_loaded_version_changes_only_after_restart_and_rolls_back(self):
        with self.server() as base:
            self.wait_ready(base)
            self.assertEqual(self.request(base, '/generate', {'prompt': 'version'})[1]['model_id'], 'fixture-A')
            self.assertEqual(self.lab('run').returncode, 0)
            code, payload = self.request(base, '/generate', {'prompt': 'version'})
            self.assertEqual(code, 200)
            self.assertEqual(payload['model_id'], 'fixture-A', 'metadata must describe weights still in memory')
            self.assertEqual(payload['answer'], 'STUB ONLY: fixture-A')
        with self.server() as base:
            self.wait_ready(base)
            payload = self.request(base, '/generate', {'prompt': 'version'})[1]
            self.assertEqual(payload['model_id'], 'fixture-B')
            self.assertEqual(payload['answer'], 'STUB ONLY: fixture-B')
            # Original-compatible controlled rollback to the previously accepted A.
            write(self.root / 'manifest-B.json', read(self.root / 'manifest-A.json'))
            write(self.root / 'report-B.json', read(self.root / 'report-A.json'))
            self.assertEqual(self.lab('run').returncode, 0)
            self.assertEqual(self.request(base, '/generate', {'prompt': 'version'})[1]['model_id'], 'fixture-B')
        with self.server() as base:
            self.wait_ready(base)
            payload = self.request(base, '/generate', {'prompt': 'version'})[1]
            self.assertEqual(payload['model_id'], 'fixture-A')
            self.assertEqual(payload['answer'], 'STUB ONLY: fixture-A')
            self.assertEqual(payload['runtime_sha256'], read(self.root / 'manifest-A.json')['runtime_sha256'])
        print(json.dumps({'kind': 'rollback-proof', 'loaded_versions': ['fixture-A', 'fixture-B', 'fixture-A']}), flush=True)

    def test_http_invalid_requests_do_not_call_backend(self):
        with self.server() as base:
            self.wait_ready(base)
            before = self.request(base, '/debug')[1]['calls']
            invalid = [{'prompt': ''}, {'prompt': '   '}, {'prompt': 'x', 'max_new_tokens': 0},
                       {'prompt': 'x', 'max_new_tokens': 100000}, {'prompt': 'x', 'max_new_tokens': '32'},
                       {'prompt': 'x', 'max_new_tokens': True}, {'prompt': 123}, {'prompt': 'x' * 8193}]
            for payload in invalid:
                with self.subTest(request=str(payload)[:100]):
                    code, _ = self.request(base, '/generate', payload)
                    self.assertTrue(400 <= code < 500, f'invalid request unexpectedly returned {code}')
                    self.assertEqual(self.request(base, '/debug')[1]['calls'], before)
            code, payload = self.request(base, '/generate', {'prompt': 'valid', 'max_new_tokens': 32})
            self.assertEqual(code, 200)
            self.assertEqual(self.request(base, '/debug')[1]['calls'], before + 1)
            self.assertEqual(payload['output_tokens'], 1)
            self.assertTrue(payload['request_id'])
            self.assertGreaterEqual(payload['generation_seconds'], 0)

    def test_atomic_replace_failure_preserves_pointer(self):
        spec = importlib.util.spec_from_file_location('release_under_test', TARGET / 'release.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = self.root / 'accepted.json'
        before = target.read_bytes()
        with patch('os.replace', side_effect=OSError('injected replace failure')):
            with self.assertRaises(OSError):
                module.publish(read(self.root / 'manifest-B.json'), read(self.root / 'report-B.json'), target)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
