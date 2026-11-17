"""No-weight provenance checks against retained real evaluation evidence.

Negative cases mutate copies of measured outputs/reports. They never synthesize
successful model answers or grant PASS to the fixture model.
"""
import json
from pathlib import Path
import tempfile
import unittest

import gate_verify
import release

EVIDENCE = Path(__file__).resolve().parents[1] / 'evidence' / 'real'


class RealGateEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (EVIDENCE / 'release-report.json').is_file():
            raise unittest.SkipTest('Real evaluation evidence has not been recorded yet')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hw8910-gate-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = json.loads((EVIDENCE / 'manifest-candidate.json').read_text())
        self.report = json.loads((EVIDENCE / 'release-report.json').read_text())
        # Evidence is portable even if its recorded original path is absolute.
        for descriptor in self.report['sources'].values():
            original = EVIDENCE / 'source' / Path(descriptor['path']).name
            target = self.root / original.name
            target.write_bytes(original.read_bytes())
            descriptor['path'] = str(target)

    def mutate_source(self, name, mutate):
        descriptor = self.report['sources'][name]
        path = Path(descriptor['path'])
        value = json.loads(path.read_text())
        mutate(value)
        path.write_bytes(release.canonical(value) + b'\n')
        descriptor['sha256'] = release.file_digest(path)

    def test_recorded_real_decision_recomputed_without_weights(self):
        decision = gate_verify.verify_report(self.manifest, self.report, self.root)
        self.assertIs(decision['passed'], self.report['passed'])
        self.assertIs(self.report['synthetic'], False)

    def test_manifest_cannot_claim_different_weight_bytes(self):
        changed = 'f' * 64
        self.assertNotEqual(self.manifest['model_sha256'], changed)
        self.manifest['model_sha256'] = changed
        self.report['model_sha256'] = changed
        with self.assertRaises(ValueError):
            gate_verify.verify_report(self.manifest, self.report, self.root)

    def test_measured_rejection_cannot_be_flipped_to_pass(self):
        self.report['passed'] = not self.report['passed']
        with self.assertRaises(ValueError):
            gate_verify.verify_report(self.manifest, self.report, self.root)

    def test_rehashed_metrics_still_need_raw_answer_agreement(self):
        self.mutate_source('candidate_eval', lambda value: value['overall'].update(accuracy=.123456))
        self.report['metrics']['accuracy'] = .123456
        with self.assertRaises(ValueError):
            gate_verify.verify_report(self.manifest, self.report, self.root)

    def test_rehashed_raw_answers_still_need_eval_digest(self):
        self.mutate_source('candidate_outputs', lambda value: value[0].update(raw='INVALID MUTATED OUTPUT'))
        with self.assertRaises(ValueError):
            gate_verify.verify_report(self.manifest, self.report, self.root)

    def test_rehashed_runtime_still_needs_manifest_agreement(self):
        def change(value):
            value['runtime']['generation']['max_new_tokens'] = 31
            value['runtime_sha256'] = release.digest(release.canonical(value['runtime']))
        self.mutate_source('candidate_eval', change)
        with self.assertRaises(ValueError):
            gate_verify.verify_report(self.manifest, self.report, self.root)

    def test_baseline_is_the_trusted_preexisting_version(self):
        baseline = json.loads(Path(self.report['sources']['baseline_eval']['path']).read_text())
        baseline['model']['revision'] = 'f' * 40
        contract = gate_verify._load_module(
            'contract_for_negative_test', EVIDENCE.parents[2] / 'hw67' / 'evaluation' / 'contract.py')
        changed_hash = contract.fingerprint(baseline['model'])
        self.mutate_source('baseline_eval', lambda value: value.update(
            model=baseline['model'], baseline_spec_sha256=changed_hash))
        self.mutate_source('candidate_eval', lambda value: value.update(baseline_spec_sha256=changed_hash))
        # Internally consistent metadata cannot substitute a different baseline.
        with self.assertRaises(ValueError):
            gate_verify.verify_report(self.manifest, self.report, self.root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
