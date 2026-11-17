"""Read monitoring evidence and calibrate histogram/client timing in a quiet stand.

Run only when no experiments are active. Uses one of the four generator Pods.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/monitoring'
KUBECTL = ROOT / '.tools/bin/kubectl'


def k(*args, check=True, timeout=180):
    proc = subprocess.run([str(KUBECTL), '-n', 'hw11', *args], text=True,
                          capture_output=True, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError(proc.stderr + proc.stdout)
    return proc.stdout


def remote(code, timeout=120):
    return k('exec', '-i', 'hw11-client-0', '--', 'python', '-c', code, timeout=timeout)


def run(calibrate=False):
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ['hw11-prometheus', 'hw11-grafana', 'hw11-kube-state-metrics']:
        k('rollout', 'status', 'deployment/' + name, '--timeout=180s', timeout=190)
    code = '''
import json,time,urllib.request,urllib.parse

def get(url):
 with urllib.request.urlopen(url,timeout=20) as r: return json.load(r)

def q(expr):
 return get('http://hw11-prometheus:9090/api/v1/query?'+urllib.parse.urlencode({'query':expr}))
result={'unix':time.time(),'targets':get('http://hw11-prometheus:9090/api/v1/targets'),
'grafana_health':get('http://hw11-grafana:3000/api/health'),
'dashboard':get('http://hw11-grafana:3000/api/dashboards/uid/hw11-kubernetes'),
'queries':{expr:q(expr) for expr in ['up','hw11_ready','kube_pod_status_ready{namespace="hw11",condition="true"}',
'container_memory_working_set_bytes{namespace="hw11",container="api"}',
'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="hw11",container="api"}[1m]))',
'hw11_ttfc_seconds_count']}}
print(json.dumps(result))
'''
    result = json.loads(remote(code))
    (OUT / 'runtime.json').write_text(json.dumps(result, indent=2))
    targets = result['targets']['data']['activeTargets']
    checks = {
        'api_scrape_2_pods':len([t for t in targets if t['labels']['job']=='hw11-api' and t['health']=='up']) == 2,
        'cadvisor_scrape':any(t['labels']['job']=='kubelet-cadvisor' and t['health']=='up' for t in targets),
        'kube_state_metrics_scrape':any(t['labels']['job']=='kube-state-metrics' and t['health']=='up' for t in targets),
        'grafana_health':result['grafana_health'].get('database') == 'ok',
        'dashboard_12_panels':len(result['dashboard']['dashboard']['panels']) == 12,
    }
    if calibrate:
        code = '''
import json,time,urllib.request,urllib.parse,uuid

def get(url):
 with urllib.request.urlopen(url,timeout=20) as r: return json.load(r)
def q(expr):
 return get('http://hw11-prometheus:9090/api/v1/query?'+urllib.parse.urlencode({'query':expr}))
time.sleep(7)
start=time.time(); before=q('hw11_ttfc_seconds_bucket'); rows=[]
for i in range(20):
 payload={'request_id':'calibration-'+str(i),'session_id':'calibration','prompt':'CALIBRATION-control-prefix '+str(i), 'max_chunks':3,'chunk_seconds':.03}
 req=urllib.request.Request('http://hw11-normal:8000/generate',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Connection':'close'})
 sent=time.monotonic();first=None;chunks=0;events=[]
 with urllib.request.urlopen(req,timeout=20) as r:
  status=r.status
  for line in r:
   event=json.loads(line);events.append(event)
   if not event.get('done'):
    if first is None:first=time.monotonic()
    chunks+=1
  completed=time.monotonic()
 rows.append({'request_id':payload['request_id'],'sent_monotonic':sent,'first_monotonic':first,'completed_monotonic':completed,'status':status,'chunks':chunks,'pod':events[0]['pod'],'model_version':events[0]['model_version'],'ttfc_seconds':first-sent,'total_seconds':completed-sent,'done':events[-1].get('done',False)})
time.sleep(8)
end=time.time(); after=q('hw11_ttfc_seconds_bucket')
expr='histogram_quantile(0.95,sum by (le) (hw11_ttfc_seconds_bucket @ %.3f - hw11_ttfc_seconds_bucket @ %.3f))'%(end,start)
print(json.dumps({'start_unix':start,'end_unix':end,'before':before,'after':after,'rows':rows,'promql':expr,'prometheus_p95':q(expr)}))
'''
        calibration = json.loads(remote(code, timeout=100))
        logs = k('logs', '-l', 'app=hw11-api', '--prefix=true', '--tail=-1')
        selected = []
        for line in logs.splitlines():
            pos=line.find('{')
            if pos < 0: continue
            try: event=json.loads(line[pos:])
            except json.JSONDecodeError: continue
            if (event.get('event')=='request_complete' and event.get('request_id','').startswith('calibration-')
                    and event.get('time_epoch',0)>=calibration['start_unix']):
                selected.append(event)
        calibration['server_logs'] = selected
        def series(data):
            return {tuple(sorted(s['metric'].items())):float(s['value'][1]) for s in data['data']['result']}
        before=series(calibration['before']);after=series(calibration['after'])
        buckets={}
        for key,val in after.items():
            labels=dict(key)
            buckets[labels['le']]=buckets.get(labels['le'],0)+val-before.get(key,0)
        calibration['exact_bucket_deltas']=buckets
        checks['calibration_20_completed']=len(calibration['rows'])==20 and all(r['done'] for r in calibration['rows'])
        checks['calibration_20_server_logs']=len(selected)==20
        checks['calibration_20_histogram_observations']=buckets.get('+Inf')==20
        def percentile(vals,p):
            vals=sorted(vals); x=(len(vals)-1)*p; lo=int(x);hi=min(lo+1,len(vals)-1)
            return vals[lo]*(hi-x)+vals[hi]*(x-lo) if lo!=hi else vals[lo]
        calibration['client_p95_seconds']=percentile([r['ttfc_seconds'] for r in calibration['rows']],.95)
        calibration['server_p95_seconds']=percentile([r['ttfc_seconds'] for r in selected],.95) if selected else None
        (OUT/'calibration.json').write_text(json.dumps(calibration,indent=2))
    (OUT/'checks.json').write_text(json.dumps(checks,indent=2))
    print(json.dumps(checks, indent=2))
    if not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--calibrate',action='store_true');args=parser.parse_args()
    run(args.calibrate)
