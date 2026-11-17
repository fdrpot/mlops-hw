"""Small release contracts; no model, server or orchestration dependencies."""
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

SHA256 = re.compile(r'^[0-9a-f]{64}$')
BINDINGS = ('model_id', 'model_sha256', 'protocol_sha256', 'golden_sha256', 'runtime_sha256')


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_digest(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def cache_key(data_path: Path, config: dict) -> str:
    return digest(canonical({'data_sha256': file_digest(data_path), 'config': config}))


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_release(manifest: dict, report: dict) -> None:
    """Validate metadata only; verify_artifact separately verifies actual bytes."""
    if not isinstance(manifest, dict) or not isinstance(report, dict):
        raise ValueError('Manifest and report must be objects')
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ValueError('schema_version must be integer 1')
    for name in ('model_id', 'artifact_uri'):
        if not _nonempty(manifest.get(name)):
            raise ValueError(f'Missing or empty {name}')
    for name in BINDINGS[1:]:
        value = manifest.get(name)
        if not isinstance(value, str) or SHA256.fullmatch(value) is None:
            raise ValueError(f'Invalid {name}')
    runtime = manifest.get('runtime')
    if not isinstance(runtime, dict) or not runtime:
        raise ValueError('runtime must be a nonempty object')
    try:
        runtime_hash = digest(canonical(runtime))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('runtime is not canonical JSON') from exc
    if runtime_hash != manifest['runtime_sha256']:
        raise ValueError('runtime_sha256 does not match runtime')
    if report.get('status') != 'complete' or report.get('passed') is not True:
        raise ValueError('Release requires a complete report with boolean passed=True')
    if not _nonempty(report.get('evaluation_run_id')):
        raise ValueError('Missing evaluation_run_id')
    for name in BINDINGS:
        if report.get(name) != manifest[name]:
            raise ValueError(f'Report binding mismatch: {name}')
    metrics = report.get('metrics')
    if not isinstance(metrics, dict):
        raise ValueError('Missing metrics')
    for name in ('accuracy', 'format_rate'):
        value = metrics.get(name)
        if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
            raise ValueError(f'{name} must be a finite number between 0 and 1')


def atomic_write(path: Path, payload: bytes) -> None:
    """Replace a file only after all new bytes are durable; one writer is assumed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', prefix=path.name + '.', suffix='.tmp',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish(manifest: dict, report: dict, target: Path) -> None:
    validate_release(manifest, report)
    atomic_write(Path(target), canonical(manifest) + b'\n')


def verify_artifact(manifest: dict, root: Path) -> Path:
    """Rehash the pinned files, allowing Hugging Face's content-addressed symlinks.

    File names must be relative and may not traverse upwards. Symlink targets can
    live in the HF blob store: expected hashes, not their location, pin the bytes.
    A single-file artifact uses file_hashes relative to its containing directory.
    """
    uri = manifest.get('artifact_uri')
    if not _nonempty(uri):
        raise ValueError('Missing artifact_uri')
    parsed = urlparse(uri)
    if parsed.scheme == 'file':
        if parsed.netloc not in ('', 'localhost'):
            raise ValueError('Only local file URIs are supported')
        artifact = Path(unquote(parsed.path))
    elif parsed.scheme:
        raise ValueError('Resolve remote artifact URI to pinned local files first')
    else:
        artifact = Path(uri)
    if not artifact.is_absolute():
        artifact = Path(root) / artifact
    if not artifact.exists():
        raise ValueError(f'Artifact does not exist: {artifact}')
    base = artifact if artifact.is_dir() else artifact.parent
    files = manifest.get('file_hashes')
    if not isinstance(files, dict) or not files:
        raise ValueError('A nonempty pinned file_hashes mapping is required')
    actual = {}
    for name, expected in files.items():
        if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('Artifact file paths must be relative without traversal')
        if not isinstance(expected, str) or SHA256.fullmatch(expected) is None:
            raise ValueError(f'Invalid pinned file hash: {name}')
        path = base / name
        if not path.is_file():
            raise ValueError(f'Missing pinned artifact file: {name}')
        actual[name] = file_digest(path)
        if actual[name] != expected:
            raise ValueError(f'Artifact file hash mismatch: {name}')
    if digest(canonical(actual)) != manifest.get('model_sha256'):
        raise ValueError('Artifact aggregate model_sha256 mismatch')
    if artifact.is_file() and artifact.name not in files:
        raise ValueError('Single-file artifact must be included in file_hashes')
    return artifact.resolve()


verify_artifacts = verify_artifact
