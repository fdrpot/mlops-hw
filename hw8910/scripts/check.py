"""One lightweight check command; no model packages, model downloads or weights.

--inject-bug bypasses validation only inside this process and runs an unchanged
public regression. It deliberately returns 1; source files remain untouched.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib
import os
from pathlib import Path
import py_compile
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def run(inject_bug):
    sources = list(ROOT.glob('*.py'))
    for directory in ('tests', 'integration', 'scripts'):
        sources.extend((ROOT / directory).rglob('*.py'))
    for path in sorted(set(sources)):
        py_compile.compile(str(path), doraise=True)
    print(f'Compiled {len(set(sources))} project Python files.', flush=True)
    sys.path.insert(0, str(ROOT))
    if inject_bug:
        print('INTENTIONAL BUG: validate_release is bypassed in memory for this process.', flush=True)
        release = importlib.import_module('release')
        release.validate_release = lambda manifest, report: None
        sys.path.insert(0, str(ROOT / 'tests'))
        suite = unittest.defaultTestLoader.loadTestsFromName(
            'test_release.ReleaseContractTests.test_report_binding_mismatch_rejected')
    else:
        suite = unittest.TestSuite()
        for directory in ('tests', 'integration'):
            suite.addTests(unittest.TestLoader().discover(str(ROOT / directory)))
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    status = 0 if result.wasSuccessful() else 1
    print(f'EXIT_CODE={status}', flush=True)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inject-bug', action='store_true')
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open('w') as log:
            output = Tee(sys.stdout, log)
            with redirect_stdout(output), redirect_stderr(output):
                return run(args.inject_bug)
    return run(args.inject_bug)


if __name__ == '__main__':
    raise SystemExit(main())
