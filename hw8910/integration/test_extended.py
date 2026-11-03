"""Additional fixed-architecture checks beyond the original-compatible regressions."""
import importlib.util
import json
from pathlib import Path
import unittest

import test_integration as support


class ExtendedReleaseTests(unittest.TestCase):
    setUp = support.ReleaseIntegrationTests.setUp
    lab = support.ReleaseIntegrationTests.lab
    server = support.ReleaseIntegrationTests.server
    request = support.ReleaseIntegrationTests.request
    wait_ready = support.ReleaseIntegrationTests.wait_ready

    def test_crash_between_pointer_and_journal_reconciles_exactly_once(self):
        result = self.lab('run', '--fail-after-publish')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture-B', (self.root / 'accepted.json').read_text())
        for _ in range(2):
            result = self.lab('run')
            self.assertEqual(result.returncode, 0, result.stdout)
        events = [json.loads(row) for row in (self.root / 'publications.jsonl').read_text().splitlines()]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]['release_id'])

    def test_corrupt_model_file_never_becomes_ready(self):
        (self.root / 'model-A.txt').write_text('CORRUPTED STUB\n')
        with self.server() as base:
            self.assertEqual(self.request(base, '/health')[0], 200)
            self.assertEqual(self.request(base, '/ready')[0], 503)
            self.assertEqual(self.request(base, '/generate', {'prompt': 'x'})[0], 503)
            debug = self.request(base, '/debug')[1]
            self.assertEqual(debug['calls'], 0)
            self.assertFalse(debug['warmup_finished'])

    def test_nan_report_does_not_change_pointer(self):
        before = (self.root / 'accepted.json').read_bytes()
        report = support.read(self.root / 'report-B.json')
        report['metrics']['accuracy'] = float('nan')
        (self.root / 'report-nan.json').write_text(json.dumps(report))
        result = self.lab('run', '--report', 'report-nan.json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'accepted.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
