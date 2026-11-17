"""Read-only analysis of retained B request traces and Prometheus time series.

No Kubernetes or network access. Re-run as evidence arrives; pending runs remain
pending. Per-run percentiles use all successful requests, never Pod percentiles.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics

from loadgen import percentile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results'
FIELDS = ('index', 'offset_seconds', 'client', 'session_id', 'prompt', 'history', 'max_chunks', 'chunk_seconds')


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def lines(path):
    if not path.exists(): return []
    result=[]
    for line in path.read_text().splitlines():
        if line.strip():
            try:result.append(json.loads(line))
            except json.JSONDecodeError:pass
    return result


def latency(rows, key):
    vals=[r[key] for r in rows if r['outcome']=='completed' and r.get(key) is not None]
    return {'n':len(vals),'p50_seconds':percentile(vals,.5),'p95_seconds':percentile(vals,.95)}


def route_run(name, rows, summary):
    successful=[r for r in rows if r['outcome']=='completed']
    counts=Counter(r['pod'] for r in rows if r.get('pod'))
    topology={p['name'] for t in lines(OUT/(name+'-timeline.jsonl')) for p in t.get('pods',[]) if not p.get('deleted')}
    for pod in topology:counts.setdefault(pod,0)
    byclient=defaultdict(Counter)
    for row in rows:
        if row.get('pod'):byclient[str(row.get('client'))][row['pod']]+=1
    trace=[{field:r.get(field) for field in FIELDS} for r in sorted(rows,key=lambda r:r['index'])]
    scheduled=read(OUT/(name+'-schedule.json'),[])
    identity_counts=Counter((r.get('request_id'),r.get('attempt',1)) for r in rows)
    return {
        'summary_available':bool(summary),
        'measurement_seconds':summary.get('measurement_seconds'),
        'raw_rows':len(rows),'summary_planned':summary.get('planned'),
        'raw_matches_summary':len(rows)==summary.get('planned'),
        'scheduled_fields_match_raw':trace==[{field:r.get(field) for field in FIELDS} for r in scheduled],
        'duplicate_attempt_keys':[{'request_id':key[0],'attempt':key[1],'rows':n} for key,n in identity_counts.items() if n>1],
        'outcomes':dict(Counter(r['outcome'] for r in rows)),
        'http_5xx':sum(500<=(r.get('status') or 0)<600 for r in rows),
        'error_fraction':(len(rows)-len(successful))/len(rows) if rows else None,
        'by_pod':dict(counts),'actual_api_pods':sorted(topology),
        'busiest_pod_share':max(counts.values())/sum(counts.values()) if counts else None,
        'by_client_pod':{client:dict(counter) for client,counter in sorted(byclient.items())},
        'sent_by_client':dict(Counter(str(r.get('client')) for r in rows)),
        'ttfc':latency(rows,'ttfc_seconds'),'queue':latency(rows,'queue_seconds'),
        'total':latency(rows,'total_seconds'),'send_lag':latency(rows,'send_lag_seconds'),
        'cache_hit_fraction':sum(bool(r.get('cache_hit')) for r in successful)/len(successful) if successful else None,
        'schedule_sha256':summary.get('schedule_sha256'),
    }


def reconstructed_idle(groups):
    examples=[]
    for run,rows in sorted(groups.items()):
        if not run.startswith('b-') or '-sticky-' not in run or any(r['outcome']!='completed' and not(r['outcome']=='rejected' and r.get('status')==429) for r in rows):continue
        pods={p['name'] for t in lines(OUT/(run+'-timeline.jsonl')) for p in t.get('pods',[]) if not p.get('deleted')}
        if len(pods)!=2:continue
        found=0
        for row in rows:
            if row['outcome']!='completed':continue
            queue=row.get('queue_seconds') or 0
            if queue<.5:continue
            when=row['sent_unix']+queue/2
            other=next(pod for pod in pods if pod!=row['pod'])
            if not any(r['sent_unix']+(r.get('queue_seconds') or 0)<=when<r['completed_unix'] for r in rows if r['pod']==other):
                examples.append({'run':run,'request_id':row['request_id'],'waiting_pod':row['pod'],
                    'queue_seconds':queue,'other_pod':other,'estimated_idle_at_unix':when})
                found+=1
                if found==3:break
    return {'method':'Approximation from client wall-clock intervals [sent_unix+server_queue_seconds,completed_unix]. All attempts must be complete or explicitly rejected HTTP429 (no backend work). Network/clock boundary uncertainty remains; Prometheus samples are the authoritative simultaneous-gauge check.',
            'examples':examples}


def prometheus_idle():
    export=read(OUT/'monitoring/timeseries.json',{})
    queries=export.get('queries',{})
    samples=defaultdict(dict)
    for metric in ('running','waiting'):
        for series in queries.get(metric,{}).get('response',{}).get('data',{}).get('result',[]):
            pod=series.get('metric',{}).get('pod')
            if not pod:continue
            for stamp,value in series.get('values',[]):
                try:value=float(value)
                except (TypeError,ValueError):continue
                samples[float(stamp)].setdefault(pod,{})[metric]=value
    windows=[]
    periods=[(p.name.removesuffix('-summary.json'),read(p,{})) for p in OUT.glob('b-*-sticky-*-summary.json')]
    topology={name:{p['name'] for t in lines(OUT/(name+'-timeline.jsonl')) for p in t.get('pods',[]) if not p.get('deleted')} for name,meta in periods}
    # A gauge must exist for both Pods at exactly the same exported timestamp.
    for stamp,pods in sorted(samples.items()):
        active_runs=[name for name,meta in periods if meta.get('start_unix',float('inf'))<=stamp<=meta.get('end_unix',0)]
        if not active_runs:continue
        allowed=set().union(*(topology[name] for name in active_runs))
        pods={pod:values for pod,values in pods.items() if pod in allowed}
        for waiting_pod,values in pods.items():
            if values.get('waiting',0)<=0:continue
            for other,other_values in pods.items():
                if other!=waiting_pod and other_values.get('running')==0 and other_values.get('waiting')==0:
                    windows.append({'unix':stamp,'runs':active_runs,'waiting_pod':waiting_pod,'waiting':values['waiting'],
                                    'running':values.get('running'),'idle_pod':other})
    return {'source':'results/monitoring/timeseries.json','export_available':bool(export),
            'step_seconds':export.get('step_seconds'),'simultaneous_samples':len(windows),
            'samples_by_run':dict(Counter(run for sample in windows for run in sample['runs'])),
            'hot80_samples':[sample for sample in windows if any(run.startswith('b-hot80-') for run in sample['runs'])][:10],
            'first_samples':windows[:20]}


def main():
    rows=[r for r in lines(OUT/'requests.jsonl') if r.get('run','').startswith('b-')]
    groups=defaultdict(list)
    for row in rows:groups[row['run']].append(row)
    summaries={p.name.removesuffix('-summary.json'):read(p,{}) for p in OUT.glob('b-*-summary.json')}
    runs={name:route_run(name,data,summaries.get(name,{})) for name,data in sorted(groups.items())}
    expected=[f'b-{traffic}-{policy}-{repeat}' for traffic in ('uniform','hot80') for policy in ('sticky','normal') for repeat in (1,2,3)]
    completed=[name for name in expected if runs.get(name,{}).get('summary_available') and (runs[name].get('measurement_seconds') or 0)>=120]
    pairs=[]
    for traffic in ('uniform','hot80'):
        for repeat in (1,2,3):
            sticky=f'b-{traffic}-sticky-{repeat}';normal=f'b-{traffic}-normal-{repeat}'
            if sticky not in runs or normal not in runs:continue
            left,right=runs[sticky],runs[normal]
            lp,rp=left['ttfc']['p95_seconds'],right['ttfc']['p95_seconds']
            pairs.append({'traffic':traffic,'repeat':repeat,'identical_schedules':left['schedule_sha256']==right['schedule_sha256'],
                          'sticky_p95_ttfc_seconds':lp,'normal_p95_ttfc_seconds':rp,
                          'p95_reduction_fraction':(lp-rp)/lp if lp else None,
                          'sticky_error_fraction':left['error_fraction'],'normal_error_fraction':right['error_fraction'],
                          'sticky_busiest_pod_share':left['busiest_pod_share'],'normal_busiest_pod_share':right['busiest_pod_share']})
    aggregates=[]
    for traffic in ('uniform','hot80'):
        for policy in ('sticky','normal'):
            names=[name for name in completed if name.startswith(f'b-{traffic}-{policy}-')]
            subset=[row for name in names for row in groups[name]]
            run_p95=[runs[name]['ttfc']['p95_seconds'] for name in names if runs[name]['ttfc']['p95_seconds'] is not None]
            aggregates.append({'traffic':traffic,'policy':policy,'complete_repeats':len(names),'runs':names,
                               'individual_run_p95_ttfc_seconds':run_p95,
                               'median_of_run_p95_seconds':statistics.median(run_p95) if run_p95 else None,
                               'pooled_request_ttfc':latency(subset,'ttfc_seconds'),
                               'pooled_request_queue':latency(subset,'queue_seconds'),
                               'outcomes':dict(Counter(r['outcome'] for r in subset))})
    no_skew=[{'run':name,'busiest_pod_share':r['busiest_pod_share'],'by_client_pod':r['by_client_pod']}
             for name,r in runs.items() if name.startswith('b-uniform-sticky-')
             and r['busiest_pod_share'] is not None and r['busiest_pod_share']<=.65]
    result={'generated_utc':datetime.now(timezone.utc).isoformat(),
            'completed_required_runs':len(completed),'required_runs':len(expected),
            'pending_runs':[name for name in expected if name not in completed],
            'runs':runs,'paired_comparisons':pairs,'aggregates':aggregates,
            'no_skew_uniform_sticky_cases':no_skew,
            'no_skew_explanation':'ClientIP fixes each source IP to one endpoint; four equal-rate clients can hash to two Pods in a 2:2 split. The raw client→Pod mapping, rather than the policy name, explains this observed no-skew case. It does not establish that hot80 is balanced.',
            'reconstructed_idle':reconstructed_idle(groups),'prometheus_simultaneous_idle':prometheus_idle(),
            'method':'Per-run latency quantiles over successful request cohorts. Pooled request quantiles and median of run p95 are explicitly separate. No averaging of Pod quantiles. No network or control-plane calls.'}
    (OUT/'route-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'completed_runs':len(completed),'pending_runs':len(expected)-len(completed),
                      'no_skew_uniform_sticky':[r['run'] for r in no_skew],
                      'prometheus_idle_samples':result['prometheus_simultaneous_idle']['simultaneous_samples']},ensure_ascii=False))


if __name__=='__main__':main()
