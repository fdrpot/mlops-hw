"""Export retained Prometheus series for the actual experiment interval.

Uses the existing client Pod, not a port-forward; does not mutate the stand.
Run promptly after experiments because retention is intentionally one hour.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
QUERIES = {
    'running': 'hw11_running',
    'waiting': 'hw11_waiting',
    'backend_ready': 'hw11_ready',
    'kubernetes_ready': 'kube_pod_status_ready{namespace="hw11",condition="true",pod=~"hw11-api-.*"}',
    'deployment_ready': 'kube_deployment_status_replicas_available{namespace="hw11",deployment="hw11-api"}',
    'cpu_cores': 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="hw11",container="api"}[30s]))',
    'ram_bytes': 'sum by (pod) (container_memory_working_set_bytes{namespace="hw11",container="api"})',
    'completed_per_second': 'sum by (pod) (rate(hw11_requests_total{outcome="complete"}[30s]))',
    'terminal_per_second': 'sum by (pod,outcome) (rate(hw11_requests_total[30s]))',
    'ttfc_p50_seconds': 'histogram_quantile(0.50,sum by (le,pod) (rate(hw11_ttfc_seconds_bucket[30s])))',
    'ttfc_p95_seconds': 'histogram_quantile(0.95,sum by (le,pod) (rate(hw11_ttfc_seconds_bucket[30s])))',
    'queue_p95_seconds': 'histogram_quantile(0.95,sum by (le,pod) (rate(hw11_queue_seconds_bucket[30s])))',
    'total_p95_seconds': 'histogram_quantile(0.95,sum by (le,pod) (rate(hw11_total_seconds_bucket[30s])))',
    'cache_per_second': 'sum by (pod,result) (rate(hw11_cache_total[30s]))',
    'hpa_current': 'kube_horizontalpodautoscaler_status_current_replicas{namespace="hw11"}',
    'hpa_desired': 'kube_horizontalpodautoscaler_status_desired_replicas{namespace="hw11"}',
    'alerts': 'ALERTS{alertname=~"HW11.*"}',
}


def inferred_start():
    requests = ROOT/'results/requests.jsonl'
    if not requests.exists():
        raise ValueError('No results/requests.jsonl; pass --start explicitly')
    stamps=[]
    for line in requests.open():
        if line.strip():
            r=json.loads(line)
            stamps.extend(float(r[k]) for k in ('planned_unix','sent_unix') if r.get(k) is not None)
    if not stamps: raise ValueError('Request log has no absolute timestamps')
    return min(stamps)-10


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start',type=float,help='UTC Unix timestamp; default earliest request minus 10 s')
    parser.add_argument('--end',type=float,help='UTC Unix timestamp; default now')
    parser.add_argument('--step',type=float,default=5.)
    parser.add_argument('--client',default='hw11-client-0')
    parser.add_argument('--namespace',default='hw11')
    parser.add_argument('--output',type=Path,default=ROOT/'results/monitoring/timeseries.json')
    args=parser.parse_args()
    start=args.start if args.start is not None else inferred_start()
    end=args.end if args.end is not None else time.time()
    if end<=start or args.step<=0 or (end-start)/args.step>11000:
        parser.error('Require start < end, step > 0 and <=11000 evaluation points')
    inputs={'start':start,'end':end,'step':args.step,'queries':QUERIES}
    # Pass source through stdin; no shell interpolation of query strings.
    code='''import json,urllib.request,urllib.parse,time\ninputs='''+repr(inputs)+'''\nresult={'captured_unix':time.time(),'start_unix':inputs['start'],'end_unix':inputs['end'],'step_seconds':inputs['step'],'backend_mode':'simulated','queries':{}}\nfor name,expr in inputs['queries'].items():\n url='http://hw11-prometheus:9090/api/v1/query_range?'+urllib.parse.urlencode({'query':expr,'start':inputs['start'],'end':inputs['end'],'step':inputs['step']})\n try:\n  with urllib.request.urlopen(url,timeout=30) as response: data=json.load(response)\n  result['queries'][name]={'promql':expr,'response':data}\n except Exception as ex: result['queries'][name]={'promql':expr,'error':str(ex)}\nprint(json.dumps(result))\n'''
    proc=subprocess.run([str(ROOT/'.tools/bin/kubectl'),'-n',args.namespace,'exec','-i',args.client,
                         '--','python','-'],input=code,text=True,capture_output=True,timeout=240)
    if proc.returncode:
        print(proc.stderr,file=sys.stderr);raise SystemExit(proc.returncode)
    result=json.loads(proc.stdout)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    counts={name:len(value.get('response',{}).get('data',{}).get('result',[])) for name,value in result['queries'].items()}
    print(json.dumps({'path':str(args.output),'start':start,'end':end,'series_counts':counts},indent=2))
    if any('error' in v or v.get('response',{}).get('status')!='success' for v in result['queries'].values()):
        raise SystemExit('Some queries failed; raw errors retained')


if __name__=='__main__':main()
