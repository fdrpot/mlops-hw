"""Open-loop NDJSON streaming client; no automatic retry or HTTP keep-alive.

This module uses only Python's standard library so exactly the same client runs
in each Kubernetes client Pod. Every scheduled request is retained, including
requests rejected before a stream and streams without a terminal done frame.
"""
from __future__ import annotations
import argparse
import asyncio
import collections
import concurrent.futures
import http.client
import json
import math
from pathlib import Path
import socket
import time
import urllib.error
import urllib.request


def schedule(duration=120.0, rps=3.0, traffic='uniform', chunks=3, chunk_seconds=.15):
    if duration <= 0 or rps <= 0:
        raise ValueError('duration and rps must be positive')
    records = []
    for i in range(math.ceil(duration * rps)):
        client = i % 4 if traffic == 'uniform' else (0 if i % 10 < 8 else 1 + (i // 10 * 2 + i % 10 - 8) % 3)
        records.append({'index': i, 'offset_seconds': i / rps, 'client': client,
                        'session_id': f'client-{client}-dialog-{i % 8}',
                        'prompt': f'prefix-{i % 8}: ' + 'shared immutable context; ' * 4 + f'input {i:05d}',
                        'history': [{'role': 'user', 'content': f'CONTROL_CLIENT_{client}'}],
                        'max_chunks': chunks, 'chunk_seconds': chunk_seconds})
    return records


def perform(url, item, planned_mono, planned_unix, run, timeout=65.0):
    start = time.monotonic()
    row = dict(item)
    row.update(run=run, request_id=f'{run}-{item["index"]}', attempt=1,
               planned_monotonic=planned_mono, planned_unix=planned_unix,
               sent_monotonic=start, sent_unix=time.time(), send_lag_seconds=max(0., start-planned_mono),
               first_monotonic=None, completed_monotonic=None, status=None,
               outcome='transport_error', chunks=0, inter_chunk_seconds=[], pod=None,
               model_version=None, backend_mode=None, queue_seconds=None,
               cache_hit=None, done=False, error=None, text='')
    payload = {key: item[key] for key in ('session_id', 'prompt', 'history', 'max_chunks', 'chunk_seconds') if key in item}
    payload['request_id'] = row['request_id']
    headers = {'Content-Type': 'application/json', 'Connection': 'close', 'X-Request-ID': row['request_id']}
    req = urllib.request.Request(url.rstrip('/') + '/generate', data=json.dumps(payload).encode(), headers=headers)
    previous = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            row['status'] = response.status
            for raw in response:
                if not raw.strip():
                    continue
                now = time.monotonic()
                frame = json.loads(raw)
                for key in ('pod', 'model_version', 'backend_mode', 'queue_seconds', 'cache_hit'):
                    if key in frame:
                        row[key] = frame[key]
                if frame.get('done'):
                    row['done'] = True
                    row['done_chunks'] = frame.get('chunks')
                    continue
                if row['first_monotonic'] is None:
                    row['first_monotonic'] = now
                if previous is not None:
                    row['inter_chunk_seconds'].append(now - previous)
                previous = now
                row['chunks'] += 1
                row['text'] += str(frame.get('text', ''))
            row['outcome'] = 'completed' if row['done'] else 'aborted'
            if not row['done']:
                row['error'] = 'Stream ended without terminal done frame'
    except urllib.error.HTTPError as exc:
        row.update(status=exc.code, outcome='rejected', error=exc.read(8192).decode(errors='replace'))
        exc.close()
        try:
            frame = json.loads(row['error'])
            for key in ('pod', 'model_version', 'backend_mode'):
                row[key] = frame.get(key)
        except (ValueError, AttributeError):
            pass
    except (TimeoutError, socket.timeout) as exc:
        row.update(outcome='timeout', error=f'{type(exc).__name__}: {exc}')
    except (OSError, ValueError, http.client.HTTPException, urllib.error.URLError) as exc:
        timed_out = isinstance(getattr(exc, 'reason', None), (TimeoutError, socket.timeout))
        row.update(outcome='timeout' if timed_out else ('aborted' if row['status'] == 200 else 'transport_error'),
                   error=f'{type(exc).__name__}: {exc}')
    end = time.monotonic()
    row['completed_monotonic'] = end
    row['completed_unix'] = time.time()
    row['total_seconds'] = end - start
    row['ttfc_seconds'] = None if row['first_monotonic'] is None else row['first_monotonic'] - start
    intervals = row['inter_chunk_seconds']
    row['mean_inter_chunk_seconds'] = sum(intervals) / len(intervals) if intervals else None
    return row


def percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def summarize(rows):
    counts = collections.Counter(row['outcome'] for row in rows)
    successful = [row for row in rows if row['outcome'] == 'completed']
    n = len(rows)
    result = {'planned': n, 'sent': sum(row.get('sent_monotonic') is not None for row in rows),
              'completed': counts['completed'], 'rejected': counts['rejected'],
              'aborted': counts['aborted'], 'timeouts': counts['timeout'],
              'transport_errors': counts['transport_error'],
              'http_5xx': sum(500 <= (row.get('status') or 0) < 600 for row in rows),
              'error_fraction': (n - len(successful)) / n if n else None,
              'timeout_fraction': counts['timeout'] / n if n else None,
              'by_pod': dict(collections.Counter(row.get('pod') for row in rows)),
              'successful_n': len(successful)}
    for name in ('ttfc_seconds', 'total_seconds', 'queue_seconds', 'send_lag_seconds'):
        source = rows if name == 'send_lag_seconds' else successful
        values = [row[name] for row in source if row.get(name) is not None]
        result[name] = {'n': len(values), 'p50': percentile(values, .5), 'p95': percentile(values, .95)}
    result['by_pod_latency'] = {}
    for pod in sorted({row['pod'] for row in successful if row.get('pod')}):
        pod_rows = [row for row in successful if row['pod'] == pod]
        result['by_pod_latency'][pod] = {'n': len(pod_rows)}
        for name in ('ttfc_seconds', 'queue_seconds'):
            vals = [row[name] for row in pod_rows if row.get(name) is not None]
            result['by_pod_latency'][pod][name] = {'n': len(vals), 'p50': percentile(vals, .5), 'p95': percentile(vals, .95)}
    return result


async def execute(url, records, run, start_unix=None, timeout=65., emit=True):
    # Convert one wall-clock rendezvous into this client's local monotonic clock.
    # Comparisons and latencies below never subtract clocks from different Pods.
    # A hot client's requests may wait concurrently at the server. Keep the
    # executor larger than its bounded queue so the generator remains open loop.
    asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=128))
    wall, mono = time.time(), time.monotonic()
    base_wall = start_unix if start_unix is not None else wall
    base_mono = mono + base_wall - wall
    async def one(item):
        planned = base_mono + item['offset_seconds']
        await asyncio.sleep(max(0, planned - time.monotonic()))
        result = await asyncio.to_thread(perform, url, item, planned, base_wall + item['offset_seconds'], run, timeout)
        if emit:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    return await asyncio.gather(*(one(item) for item in records))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='http://hw11-normal:8000')
    p.add_argument('--schedule', type=Path)
    p.add_argument('--client', type=int)
    p.add_argument('--run', default='manual')
    p.add_argument('--start-unix', type=float)
    p.add_argument('--duration', type=float, default=120)
    p.add_argument('--rps', type=float, default=3)
    p.add_argument('--traffic', choices=['uniform', 'hot80'], default='uniform')
    p.add_argument('--timeout', type=float, default=65)
    args = p.parse_args()
    records = json.loads(args.schedule.read_text()) if args.schedule else schedule(args.duration, args.rps, args.traffic)
    if args.client is not None:
        records = [r for r in records if r['client'] == args.client]
    asyncio.run(execute(args.url, records, args.run, args.start_unix, args.timeout))


if __name__ == '__main__':
    main()
