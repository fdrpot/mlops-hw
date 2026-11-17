"""Capture actual Prefect states, cross-process cache recovery and invalidation.

Run from hw8910: .venv/bin/python scripts/prefect_evidence.py
Choose a fresh --workspace each time; existing workspaces are never overwritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def read_runs(workspace, home):
    database = home / 'prefect.db'
    if not database.exists():
        return None
    with sqlite3.connect(database, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        for raw in connection.execute('SELECT id,name,state_type,state_name,parameters,start_time,end_time '
                                      'FROM flow_run ORDER BY created DESC LIMIT 100'):
            record = dict(raw)
            parameters = json.loads(record.pop('parameters'))
            if parameters.get('workspace') == str(workspace):
                record['parameters'] = parameters
                record['tasks'] = [dict(row) for row in connection.execute(
                    'SELECT id,name,state_type,state_name,cache_key,start_time,end_time FROM task_run '
                    'WHERE flow_run_id=? ORDER BY created', (record['id'],))]
                return record
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=PROJECT / '.lab-prefect')
    parser.add_argument('--output', type=Path, default=PROJECT / 'evidence/prefect')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    output = args.output.resolve()
    if workspace.exists():
        raise FileExistsError('Select a fresh --workspace; existing evidence workspace is preserved')
    output.mkdir(parents=True, exist_ok=True)
    home = PROJECT / '.prefect'
    environment = {**os.environ, 'PREFECT_HOME': str(home),
                   'PREFECT_SERVER_ANALYTICS_ENABLED': 'false', 'PREFECT_LOGGING_COLORS': 'false'}
    summary = {'synthetic': True, 'backend': 'STUB ONLY', 'workspace': str(workspace),
               'prefect_home': str(home), 'python': sys.executable,
               'started_unix': time.time(), 'steps': []}

    def save():
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def step(name, command, expected_exit):
        pointer = workspace / 'accepted.json'
        record = {'name': name, 'command': command, 'cwd': str(PROJECT),
                  'accepted_before_sha256': file_hash(pointer), 'started_unix': time.time()}
        completed = subprocess.run(command, cwd=PROJECT, env=environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        log = output / (name + '.log')
        log.write_text(completed.stdout, encoding='utf-8')
        record.update(exit_code=completed.returncode, expected_exit=expected_exit,
                      log=str(log.relative_to(PROJECT)), log_sha256=file_hash(log),
                      accepted_after_sha256=file_hash(pointer), finished_unix=time.time())
        if 'flow.py' in command:
            record['flow_run'] = read_runs(workspace, home)
        journal = workspace / 'publications.jsonl'
        record['publication_count'] = len(journal.read_text().splitlines()) if journal.exists() else 0
        summary['steps'].append(record)
        save()
        print(name, 'exit=' + str(completed.returncode), 'publications=' + str(record['publication_count']), flush=True)
        if completed.returncode != expected_exit:
            raise AssertionError(f'{name}: expected exit {expected_exit}, see {log}')
        return record, completed.stdout

    python = sys.executable
    lab_command = [python, 'lab.py', '--workspace', str(workspace)]
    flow_command = [python, 'flow.py', '--workspace', str(workspace)]
    step('01-init', lab_command + ['init'], 0)
    failed, _ = step('02-fail-after-prepare', flow_command + ['--fail-after-prepare'], 1)
    recovered, recovery_log = step('03-recovery', flow_command, 0)
    repeated, _ = step('04-repeat', flow_command, 0)
    source = workspace / 'input.txt'
    summary['input_before_change_sha256'] = file_hash(source)
    source.write_text(source.read_text(encoding='utf-8') + 'Третий пример после изменения байтов\n', encoding='utf-8')
    summary['input_after_change_sha256'] = file_hash(source)
    changed, changed_log = step('05-changed-input', flow_command, 0)
    checks = {
        'failure_preserved_pointer': failed['accepted_before_sha256'] == failed['accepted_after_sha256'],
        'new_process_reused_prefect_cache': 'Cached(type=COMPLETED)' in recovery_log,
        'repeat_preserved_pointer': repeated['accepted_before_sha256'] == repeated['accepted_after_sha256'],
        'repeat_has_one_publication': repeated['publication_count'] == 1,
        'changed_input_executed_preparation': 'PREPARED' in changed_log and 'Cached(type=COMPLETED)' not in changed_log,
        'changed_input_has_one_publication': changed['publication_count'] == 1,
        'different_flow_run_ids': len({row['flow_run']['id'] for row in summary['steps'] if row.get('flow_run')}) == 4,
    }
    summary.update(checks=checks, passed=all(checks.values()), finished_unix=time.time())
    save()
    if not summary['passed']:
        raise AssertionError(checks)
    print('PASS: real Prefect recovery, durable cache, invalidation and deduplication')


if __name__ == '__main__':
    main()
