"""Prefect flow over small descriptors of existing training/evaluation artifacts."""
import argparse
import os
from datetime import timedelta
from pathlib import Path

os.environ.setdefault('PREFECT_HOME', str(Path(__file__).resolve().parent / '.prefect'))
os.environ.setdefault('PREFECT_SERVER_ANALYTICS_ENABLED', 'false')

from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE

import lab
import release


def _prepare_key(context, parameters):
    root = Path(parameters['workspace']).resolve()
    # The content key is path-independent. Namespace descriptors by workspace so
    # equal data in separate experiments never returns another workspace's path.
    return release.digest(release.canonical({'workspace': str(root), 'content': lab.preparation_key(root)}))


@task(name='prepare-and-validate-input', cache_key_fn=_prepare_key,
      persist_result=True, cache_expiration=timedelta(days=30),
      result_serializer='json', retries=0, log_prints=True)
def prepare_input(workspace: str) -> dict:
    return lab.prepare(Path(workspace))


@task(name='acquire-existing-training-artifact', cache_policy=NO_CACHE,
      persist_result=False, retries=0)
def acquire_artifact(workspace: str, manifest_name: str) -> dict:
    root = Path(workspace)
    path = root / manifest_name
    manifest = lab.read(path)
    artifact = release.verify_artifact(manifest, root)
    get_run_logger().info('Reading existing artifact %s; no new training', manifest['model_id'])
    return {'manifest_path': str(path.resolve()), 'artifact_uri': str(artifact),
            'model_id': manifest['model_id'], 'model_sha256': manifest['model_sha256']}


@task(name='check-bound-full-gate-report', cache_policy=NO_CACHE,
      persist_result=False, retries=0)
def check_gate(workspace: str, artifact: dict, report_name: str) -> dict:
    root = Path(workspace)
    manifest = lab.read(artifact['manifest_path'])
    report_path = root / report_name
    report = lab.read(report_path)
    synthetic = (report.get('synthetic') is True
                 and manifest.get('model_id', '').startswith('fixture-')
                 and manifest.get('runtime', {}).get('base_revision') == 'fixture-only')
    if not synthetic:
        from gate_verify import verify_report
        verify_report(manifest, report, root)
    release.validate_release(manifest, report)
    return {'report_path': str(report_path.resolve()), 'report_sha256': release.digest(release.canonical(report)),
            'evaluation_run_id': report['evaluation_run_id'], 'passed': report['passed']}


@task(name='publish-once-and-reconcile-journal', cache_policy=NO_CACHE,
      persist_result=False, retries=0, log_prints=True)
def publish_release(workspace: str, artifact: dict, decision: dict,
                    prepared: dict, fail_after_publish: bool = False) -> dict:
    manifest = lab.read(artifact['manifest_path'])
    report = lab.read(decision['report_path'])
    if release.digest(release.canonical(report)) != decision['report_sha256']:
        raise ValueError('Report changed between gate and publication')
    return lab.publish_once(Path(workspace), manifest, report, prepared, fail_after_publish)


@flow(name='hw8910-release-existing-model', retries=0, log_prints=True,
      persist_result=False)
def release_flow(workspace: str, manifest_name: str = 'manifest-B.json',
                 report_name: str = 'report-B.json', fail_after_prepare: bool = False,
                 fail_after_publish: bool = False) -> dict:
    root = Path(workspace).resolve()
    attempt = lab.start_attempt(root)
    try:
        prepared = prepare_input(str(root))
        if release.digest(Path(prepared['prepared_path']).read_bytes()) != prepared['prepared_sha256']:
            raise ValueError('Prepared cache payload changed')
        if fail_after_prepare:
            raise RuntimeError('Инъекция сбоя после подготовки')
        artifact = acquire_artifact(str(root), manifest_name)
        decision = check_gate(str(root), artifact, report_name)
        result = publish_release(str(root), artifact, decision, prepared, fail_after_publish)
    except BaseException as exc:
        lab.finish_attempt(root, attempt, 'failed', error=f'{type(exc).__name__}: {exc}')
        raise
    lab.finish_attempt(root, attempt, 'complete', release_id=result['release_id'], published=result['published'])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.lab'))
    parser.add_argument('--manifest', default='manifest-B.json')
    parser.add_argument('--report', default='report-B.json')
    parser.add_argument('--fail-after-prepare', action='store_true')
    parser.add_argument('--fail-after-publish', action='store_true')
    args = parser.parse_args()
    release_flow(str(args.workspace), args.manifest, args.report,
                 args.fail_after_prepare, args.fail_after_publish)


if __name__ == '__main__':
    main()
