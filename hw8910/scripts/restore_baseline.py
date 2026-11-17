"""Restore the retained baseline on another machine without local MLflow volumes."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT / '.real')
    parser.add_argument('--snapshot', type=Path)
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'evidence/real/accepted.json').read_text())
    runtime = manifest['runtime']
    if runtime['implementation_sha256'] != release.file_digest(ROOT / 'real_backend.py'):
        raise ValueError('Generator code changed: reevaluate instead of restoring a stale runtime')
    if args.snapshot:
        snapshot = args.snapshot.resolve()
    else:
        from huggingface_hub import snapshot_download
        snapshot = Path(snapshot_download(
            manifest['model_spec']['name'], revision=runtime['base_revision'],
            allow_patterns=['*.json', '*.safetensors', '*.txt', '*.jinja']))
    models = ROOT / 'models'
    models.mkdir(exist_ok=True)
    base = models / 'base'
    if not base.exists():
        base.symlink_to(snapshot, target_is_directory=True)
    elif base.resolve() != snapshot:
        raise ValueError('models/base points to another snapshot')
    manifest['artifact_uri'] = str(models)
    release.verify_artifacts(manifest, ROOT)
    args.workspace.mkdir(exist_ok=True, parents=True)
    pointer = args.workspace / 'accepted.json'
    if pointer.exists() and json.loads(pointer.read_text()) != manifest:
        raise ValueError('Existing accepted pointer differs; choose a new workspace')
    release.atomic_write(pointer, release.canonical(manifest) + b'\n')
    print('RESTORED retained baseline', manifest['model_id'], manifest['runtime_sha256'])
    print('This restores an existing baseline; it does not create a quality PASS.')


if __name__ == '__main__':
    main()
