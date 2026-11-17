"""Behavioral checks exercise actual HTTP/NDJSON, not a buffered mock stream."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Config, create_app


class ArtifactAndProbeTests(unittest.TestCase):
    def test_wrong_artifact_hash_fails_closed(self):
        app = create_app(Config(warmup_seconds=0, artifact_sha256='0' * 64))
        with TestClient(app) as client:
            time.sleep(.03)
            self.assertEqual(client.get('/health').status_code, 200)
            self.assertEqual(client.get('/ready').status_code, 503)
            self.assertIn('SHA256 mismatch', client.get('/ready').json()['load_error'])
            self.assertEqual(client.post('/generate', json={'prompt': 'x'}).status_code, 503)

    def test_original_probe_bug_then_fix(self):
        for mode, status in [('broken', 200), ('fixed', 503)]:
            with self.subTest(mode=mode):
                with TestClient(create_app(Config(warmup_seconds=10, probe_mode=mode))) as client:
                    response = client.get('/startup')
                    self.assertEqual(response.status_code, status)
                    self.assertFalse(response.json()['warm'])
                    self.assertEqual(client.post('/generate', json={'prompt': 'too early'}).status_code, 503)


class LiveHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cls.base = f'http://127.0.0.1:{port}'
        cls.log = tempfile.TemporaryFile(mode='w+')
        environment = dict(os.environ, WARMUP_SECONDS='.1', CPU_SECONDS='0', HIT_SECONDS='.01',
                           MISS_SECONDS='.02', CHUNK_SECONDS='.02', QUEUE_LIMIT='0', CACHE_SIZE='1', POD_NAME='test-pod')
        cls.process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app', '--port', str(port),
                                        '--no-access-log'], cwd=Path(__file__).resolve().parents[1],
                                       env=environment, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(cls.base + '/ready').status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.02)
        cls.log.seek(0)
        raise RuntimeError(cls.log.read())

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.log.close()

    def request(self, **body):
        body.setdefault('prompt', 'test-prefix')
        response = httpx.post(self.base + '/generate', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return [json.loads(line) for line in response.text.splitlines()]

    def test_dialogs_are_isolated_and_identifiable(self):
        first = self.request(session_id='one', history=[{'role': 'user', 'content': 'CONTROL_ALPHA'}], max_chunks=1)
        second = self.request(session_id='two', history=[{'role': 'user', 'content': 'CONTROL_BETA'}], max_chunks=1)
        self.assertIn('CONTROL_ALPHA', first[0]['text'])
        self.assertNotIn('CONTROL_ALPHA', second[0]['text'])
        self.assertIn('CONTROL_BETA', second[0]['text'])
        self.assertNotIn('CONTROL_BETA', first[0]['text'])
        for frames in [first, second]:
            self.assertTrue(frames[-1]['done'])
            self.assertEqual(frames[-1]['chunks'], 1)
            self.assertEqual(frames[0]['model_version'], 'A')
            self.assertEqual(frames[0]['pod'], 'test-pod')
            self.assertEqual(frames[0]['backend_mode'], 'simulated')

    def test_rejections_and_aborted_stream_have_distinct_counters(self):
        with httpx.Client() as client:
            with client.stream('POST', self.base + '/generate', json={
                    'prompt': 'long-prefix', 'request_id': 'cancel-me', 'max_chunks': 100, 'chunk_seconds': .1}) as response:
                self.assertEqual(response.status_code, 200)
                lines = response.iter_lines()
                first = next(lines)
                self.assertFalse(json.loads(first)['done'])
                rejected = client.post(self.base + '/generate', json={'prompt': 'overload'})
                self.assertEqual(rejected.status_code, 429)
            # Closing before done is an aborted HTTP200 stream, distinct from rejection.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            metrics = httpx.get(self.base + '/metrics').text
            if 'outcome="aborted",pod="test-pod"} 1.0' in metrics:
                break
            time.sleep(.02)
        self.assertIn('outcome="rejected",pod="test-pod"} 1.0', metrics)
        self.assertIn('outcome="aborted",pod="test-pod"} 1.0', metrics)
        self.assertIn('hw11_running{model_version="A",pod="test-pod"} 0.0', metrics)
        self.assertNotIn('request_id=', metrics)
        self.assertNotIn('session_id=', metrics)

    def test_validation_and_lru_cache(self):
        response = httpx.post(self.base + '/generate', json={'prompt': 'x', 'max_chunks': 0})
        self.assertEqual(response.status_code, 422)
        first = self.request(prompt='CACHE_EVICT_A', max_chunks=1)
        self.assertFalse(first[0]['cache_hit'])
        self.request(prompt='CACHE_EVICT_B', max_chunks=1)
        evicted = self.request(prompt='CACHE_EVICT_A', max_chunks=1)
        self.assertFalse(evicted[0]['cache_hit'])
        second = self.request(prompt='CACHE_EVICT_A', max_chunks=1)
        self.assertTrue(second[0]['cache_hit'])
        self.assertGreaterEqual(second[0]['queue_seconds'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
