"""Real Kubernetes experiments, retaining raw HTTP, control-plane and metrics data.

Run from hw11: python scripts/experiments.py {clients,a,b,c,all}.
The comparison is deliberately sequential: both Services target the same two
replicas, and each repeat begins with restarted, cold local prefix caches.
"""
from __future__ import annotations
import argparse
import concurrent.futures
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from loadgen import schedule, summarize

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get('HW11_BIN', ROOT / '.tools/bin'))
OUT = ROOT / 'results'
NS = os.environ.get('HW11_NAMESPACE', 'hw11')
RELEASE = 'hw11'
DEPLOY = 'hw11-api'
PODS = [f'hw11-client-{i}' for i in range(4)]


def command(args, *, input=None, check=True, timeout=240):
    tool = BIN / args[0]
    argv = [str(tool) if tool.exists() else args[0], *map(str, args[1:])]
    started_unix=time.time()
    result = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'commands.jsonl').open('a') as stream:
        stream.write(json.dumps({'started_unix':started_unix,'unix': time.time(), 'argv': argv, 'returncode': result.returncode,
                                 'stdout': result.stdout[-10000:], 'stderr': result.stderr[-10000:]}) + '\n')
    if check and result.returncode:
        raise RuntimeError(f'{args}: {result.stderr or result.stdout}')
    return result


def k(*args, **kwargs):
    return command(['kubectl', '-n', NS, *args], **kwargs)


def get(kind, name=None):
    return json.loads(k('get', kind, *([name] if name else []), '-o', 'json').stdout)


def upgrade(**values):
    args = ['helm', 'upgrade', RELEASE, str(ROOT/'chart'), '-n', NS, '--reuse-values']
    for key, value in values.items():
        args += ['--set', f'{key}={str(value).lower() if isinstance(value, bool) else value}']
    command(args)


def ready(timeout=180):
    k('rollout', 'status', 'deployment/'+DEPLOY, f'--timeout={timeout}s', timeout=timeout+10)


def servers():
    items = get('pods')['items']
    return [p for p in items if p['metadata']['name'].startswith(DEPLOY+'-') and not p['metadata'].get('deletionTimestamp')]


def remote(client, script, *, timeout=240):
    return k('exec', '-i', client, '--', 'python', '-c', script, timeout=timeout).stdout


def http(client, url, payload=None):
    script = ('import urllib.request,json; '
              f'url={url!r}; payload={payload!r}; '
              'req=urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, '
              'headers={"Content-Type":"application/json","Connection":"close"}); '
              'print(urllib.request.urlopen(req,timeout=90).read().decode())')
    return remote(client, script, timeout=100)


def setup_clients():
    for name in PODS:
        exists = k('get', 'pod', name, check=False)
        if exists.returncode:
            k('run', name, '--image=hw11-api:local', '--image-pull-policy=IfNotPresent', '--restart=Never',
              '--labels=app=hw11-client', '--overrides='+json.dumps({'spec':{'containers':[{'name':name,'image':'hw11-api:local',
                'command':['python','-c','import time; time.sleep(86400)'], 'resources':{'requests':{'cpu':'10m','memory':'32Mi'},
                  'limits':{'cpu':'500m','memory':'128Mi'}}}]}}))
        k('wait', '--for=condition=Ready', 'pod/'+name, '--timeout=90s')
        k('exec', '-i', name, '--', 'python', '-c', 'from pathlib import Path; import sys; Path("/tmp/loadgen.py").write_text(sys.stdin.read())',
          input=(ROOT/'scripts/loadgen.py').read_text())
    info = [{'name': p['metadata']['name'], 'ip': p['status'].get('podIP'),
             'node':p['spec'].get('nodeName')} for p in get('pods')['items'] if p['metadata']['name'] in PODS]
    (OUT/'clients.json').write_text(json.dumps(info, indent=2))
    assert len({r['ip'] for r in info}) == 4, info
    return info


def snapshot():
    result = get('pods,endpointslices,hpa,deployments')
    snap = {'unix':time.time(), 'monotonic':time.monotonic(), 'pods':[], 'endpoints':[], 'hpa':[], 'deployments':[]}
    for item in result['items']:
        name = item['metadata']['name']
        kind = item['kind']
        if kind == 'Pod' and name.startswith(DEPLOY+'-'):
            snap['pods'].append({'name':name, 'created':item['metadata']['creationTimestamp'],
                'deleted':item['metadata'].get('deletionTimestamp'), 'ip':item['status'].get('podIP'),
                'phase':item['status'].get('phase'), 'conditions':item['status'].get('conditions'),
                'containers':item['status'].get('containerStatuses'),
                'env':item['spec']['containers'][0].get('env')})
        elif kind == 'EndpointSlice' and name.startswith('hw11-'):
            snap['endpoints'].append({'name':name, 'service':item['metadata']['labels'].get('kubernetes.io/service-name'),
                                     'endpoints':item.get('endpoints',[])})
        elif kind == 'HorizontalPodAutoscaler':
            snap['hpa'].append({'name':name, 'spec':item.get('spec'), 'status':item.get('status')})
        elif kind == 'Deployment' and name == DEPLOY:
            snap['deployments'].append({'name':name, 'status':item.get('status')})
    return snap


@contextmanager
def observe(name):
    stop = threading.Event()
    path = OUT/(name+'-timeline.jsonl')
    log_stream=(OUT/(name+'-live-server.log')).open('a')
    log_processes={}
    def worker():
        while not stop.is_set():
            try:
                snap=snapshot()
                with path.open('a') as stream:
                    stream.write(json.dumps(snap)+'\n')
                for pod in snap['pods']:
                    pod_name=pod['name']
                    if pod_name not in log_processes or log_processes[pod_name].poll() is not None:
                        tool=BIN/'kubectl'
                        log_processes[pod_name]=subprocess.Popen([str(tool) if tool.exists() else 'kubectl',
                            '-n',NS,'logs','-f',pod_name,'--prefix=true'],stdout=log_stream,stderr=log_stream,text=True)
            except Exception as exc:
                with (OUT/'observer-errors.log').open('a') as stream:
                    stream.write(f'{time.time()} {name}: {exc}\n')
            stop.wait(2)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=15)
        for process in log_processes.values():
            if process.poll() is None:
                process.terminate()
        for process in log_processes.values():
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill()
        log_stream.close()
        logs = k('logs','-l','app=hw11-api','--all-containers=true','--prefix=true','--tail=-1',check=False)
        # Older chart label schemes still retain logs by exact Pod name.
        if not logs.stdout:
            logs = k('logs','deployment/'+DEPLOY,'--all-pods=true','--prefix=true','--tail=-1',check=False)
        (OUT/(name+'-server.log')).write_text(logs.stdout+logs.stderr)


def run_load(name, service='normal', *, duration=120., rps=3., traffic='uniform', records=None, timeout=65.):
    records = records if records is not None else schedule(duration,rps,traffic)
    path = OUT/(name+'-schedule.json')
    path.write_text(json.dumps(records,indent=2))
    schedule_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    for client in PODS:
        k('exec','-i',client,'--','python','-c',
          'from pathlib import Path; import sys; Path("/tmp/schedule.json").write_text(sys.stdin.read())', input=path.read_text())
    start = time.time()+5
    def one(index):
        client = PODS[index]
        proc = k('exec',client,'--','python','/tmp/loadgen.py','--url',
                 service if service.startswith('http') else f'http://hw11-{service}:8000',
                 '--schedule','/tmp/schedule.json','--client',str(index),'--run',name,
                 '--start-unix',str(start),'--timeout',str(timeout),timeout=int(duration+timeout+90))
        (OUT/(name+f'-client-{index}.jsonl')).write_text(proc.stdout)
        return [dict(json.loads(line),client_pod=client) for line in proc.stdout.splitlines() if line.strip()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = [row for result in pool.map(one, range(4)) for row in result]
    rows.sort(key=lambda r:r['index'])
    if len(rows)!=len(records):
        raise AssertionError(f'{name}: planned {len(records)}, rows {len(rows)}')
    with (OUT/'requests.jsonl').open('a') as stream:
        stream.writelines(json.dumps(row,ensure_ascii=False)+'\n' for row in rows)
    summary=summarize(rows)
    summary.update(run=name,service=service,traffic=traffic,measurement_seconds=duration,
                   rps=rps,schedule_sha256=schedule_hash,start_unix=start,end_unix=time.time(),
                   keep_alive=False,retries=0,backend_mode='simulated')
    (OUT/(name+'-summary.json')).write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary),flush=True)
    return rows


def cold_reset():
    # No measured load is active between runs. Restart both replicas together
    # to establish an empty local cache and identical 20s warmup for each run.
    k('scale','deployment/'+DEPLOY,'--replicas=0')
    k('wait','--for=delete','pod','-l','app=hw11-api','--timeout=90s')
    k('scale','deployment/'+DEPLOY,'--replicas=2')
    ready()


def warmup_experiment(mode):
    name='a-warmup-'+mode
    upgrade(**{'model.probeMode':mode,'model.version':'A','model.neverReady':False,'model.warmupSeconds':20,
               'hpa.enabled':False,'replicaCount':2})
    ready()
    k('scale','deployment/'+DEPLOY,'--replicas=0')
    k('wait','--for=delete','pod','-l','app=hw11-api','--timeout=90s',check=False)
    with observe(name):
        k('scale','deployment/'+DEPLOY,'--replicas=2')
        rows=run_load(name,duration=35,rps=2)
        ready()
    return rows


def experiment_a():
    broken=warmup_experiment('broken')
    fixed=warmup_experiment('fixed')
    # Confirm both outcomes from observed HTTP; unavailable Service before Ready
    # is a transport failure, while backend 503 proves premature routing.
    checks={'broken_received_backend_503':any(r['status']==503 for r in broken),
            'fixed_no_backend_503':all(r['status']!=503 for r in fixed)}
    upgrade(**{'model.version':'A','model.probeMode':'fixed','model.neverReady':False})
    ready()
    long=schedule(65,.3,chunks=3,chunk_seconds=.1)
    long[0].update(max_chunks=80,chunk_seconds=.5)
    with observe('a-rollout'):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            load=pool.submit(run_load,'a-rollout',duration=65,rps=.3,records=long)
            time.sleep(12) # client rendezvous 5s plus 7s guarantees stream in progress
            rollout_started=time.time()
            upgrade(**{'model.version':'B'})
            ready()
            rows=load.result()
    checks['graceful_rollout_zero_5xx_and_incomplete']=all(r['outcome']=='completed' and r['status']==200 for r in rows)
    checks['long_stream_crossed_rollout']=rows[0]['sent_unix']<rollout_started<rows[0]['completed_unix']
    (OUT/'a-rollout-boundary.json').write_text(json.dumps({'rollout_started_unix':rollout_started},indent=2))
    # Start from A before releasing deliberately unready B.
    upgrade(**{'model.version':'A'});ready()
    with observe('a-unready-rollback'):
        upgrade(**{'model.version':'B','model.neverReady':True})
        rows=run_load('a-unready-b',duration=35,rps=.5)
        checks['unready_b_not_serving']=all(r['model_version']=='A' for r in rows if r['outcome']=='completed')
        (OUT/'a-unready-pods.json').write_text(json.dumps(get('pods'),indent=2))
        upgrade(**{'model.version':'A','model.neverReady':False})
        ready()
        rows=run_load('a-after-rollback',duration=8,rps=1)
        checks['rollback_loaded_a']=all(r['model_version']=='A' for r in rows if r['outcome']=='completed') and any(r['outcome']=='completed' for r in rows)
    checks.update(forced_failure())
    (OUT/'a-checks.json').write_text(json.dumps(checks,indent=2))
    if not all(checks.values()):
        raise AssertionError(checks)
    return checks


def forced_failure(name='a-forced-kill'):
    """Force deletion makes kubelet/runtime kill PID 1 from its ancestor PIDNS.

    Sending os.kill(1,SIGKILL) from kubectl exec may be ignored by namespace init;
    results/a-forced-* retains that initial, unsuccessful failure injection.
    """
    initial=servers();initial_names={p['metadata']['name'] for p in initial}
    victim=initial[0];ip=victim['status']['podIP'];pod=victim['metadata']['name']
    forced=schedule(1,1,chunks=80,chunk_seconds=.5)
    boundary={'victim_pod':pod,'victim_ip':ip,'victim_uid':victim['metadata']['uid'],
              'container_id':victim['status'].get('containerStatuses',[{}])[0].get('containerID')}
    with observe(name):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            load=pool.submit(run_load,name,f'http://{ip}:8000',duration=1,rps=1,records=forced)
            time.sleep(12)
            boundary['killed_at_unix']=time.time()
            k('delete','pod',pod,'--grace-period=0','--force','--wait=false')
            boundary['kill_command_finished_unix']=time.time()
            rows=load.result()
        deadline=time.monotonic()+120
        replacement=None
        while time.monotonic()<deadline:
            current=servers()
            if not any(p['metadata']['name']==pod for p in current):
                boundary.setdefault('failure_detected_unix',time.time())
            candidates=[p for p in current if p['metadata']['name'] not in initial_names and p['status'].get('podIP')
                        and any(c['type']=='Ready' and c['status']=='True' for c in p['status'].get('conditions',[]))]
            if candidates:
                replacement=candidates[0];boundary['replacement_observed_ready_unix']=time.time();break
            time.sleep(.5)
        if replacement is None:
            raise TimeoutError('Forced replacement did not become Ready')
        boundary.update(replacement_pod=replacement['metadata']['name'],replacement_ip=replacement['status']['podIP'],
                        replacement_created=replacement['metadata']['creationTimestamp'],
                        replacement_conditions=replacement['status'].get('conditions'))
        ready()
        recovered=run_load(name+'-recovery',duration=8,rps=1)
        successful=[r for r in recovered if r['outcome']=='completed']
        boundary['first_postkill_completed_unix']=min((r['completed_unix'] for r in successful),default=None)
    (OUT/(name+'-boundary.json')).write_text(json.dumps(boundary,indent=2))
    return {'forced_stream_aborted':any(r['outcome']=='aborted' for r in rows),
            'forced_recovery':bool(successful),'forced_replacement_ready':replacement is not None}


def experiment_b(duration=120,rps=4):
    if duration<120:
        raise ValueError('Final comparison requires measurement >=120 seconds')
    upgrade(**{'hpa.enabled':False,'replicaCount':2,'model.version':'A','model.probeMode':'fixed',
               'model.neverReady':False,'model.warmupSeconds':20,'backend.maxConcurrency':1,
               'backend.queueLimit':64,'backend.queueTimeout':60,'backend.cacheSize':16,
               'backend.hitSeconds':.04,'backend.missSeconds':.20,'backend.cpuSeconds':.02})
    ready()
    (OUT/'b-services.json').write_text(json.dumps(get('services'),indent=2))
    for traffic in ('uniform','hot80'):
        records=schedule(duration,rps,traffic)
        for repeat in range(1,4):
            for service in ('sticky','normal'):
                name=f'b-{traffic}-{service}-{repeat}'
                cold_reset()
                with observe(name):
                    run_load(name,service,duration=duration,rps=rps,traffic=traffic,records=records)
    return {'measurement_seconds':duration,'repeats':3,'policies':['sticky','normal'],'traffic':['uniform','hot80']}


def wait_hpa(desired,timeout=240):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        objects=get('hpa')['items']
        if objects and objects[0].get('status',{}).get('currentReplicas')==desired:
            return time.monotonic()-start
        time.sleep(5)
    raise TimeoutError(f'HPA did not reach {desired} replicas')


def query_prom(client=PODS[0]):
    script=('import urllib.request,json,time; root="http://hw11-prometheus:9090"; '
            'print(json.dumps({"unix":time.time(),"alerts":json.load(urllib.request.urlopen(root+"/api/v1/alerts",timeout=10)), '
            '"targets":json.load(urllib.request.urlopen(root+"/api/v1/targets",timeout=10))}))')
    return json.loads(remote(client,script,timeout=30))


@contextmanager
def observe_prom(name):
    stop=threading.Event()
    def worker():
        while not stop.is_set():
            try:
                with (OUT/(name+'-prometheus.jsonl')).open('a') as stream:
                    stream.write(json.dumps(query_prom())+'\n')
            except Exception as exc:
                with (OUT/'observer-errors.log').open('a') as stream:
                    stream.write(f'{time.time()} prometheus {exc}\n')
            stop.wait(5)
    thread=threading.Thread(target=worker,daemon=True);thread.start()
    try:yield
    finally:stop.set();thread.join(timeout=35)


def experiment_c():
    (OUT/'c-resource-metrics-api.json').write_text(command(['kubectl','get','--raw','/apis/metrics.k8s.io/v1beta1/nodes']).stdout)
    upgrade(**{'hpa.enabled':True,'model.version':'A','model.neverReady':False,'model.warmupSeconds':20,
               'backend.cpuSeconds':.15,'backend.queueLimit':64,'backend.queueTimeout':60})
    ready()
    wait_hpa(2,120)
    ready()
    with observe('c-scaling'),observe_prom('c-scaling'):
        # Real CPU work in the simulated backend is the HPA stimulus.
        rows=run_load('c-scale-up',duration=180,rps=4,records=schedule(180,4,chunks=2,chunk_seconds=.05))
        up=get('hpa')
        (OUT/'c-hpa-after-load.json').write_text(json.dumps(up,indent=2))
        if not any(h.get('status',{}).get('currentReplicas')==3 for h in up['items']):
            raise AssertionError('HPA never reached three replicas; inspect c-scaling-timeline.jsonl')
        load_stopped=time.time()
        down_seconds=wait_hpa(2,300)
        (OUT/'c-scale-down.json').write_text(json.dumps({'load_stopped_unix':load_stopped,
                                                     'wait_seconds':down_seconds,'observed_unix':time.time(),
                                                     'arrival_phase_ended_unix':min(r['planned_unix'] for r in rows)+180,
                                                     'last_actual_send_unix':max(r['sent_unix'] for r in rows),
                                                     'last_completion_unix':max(r['completed_unix'] for r in rows)},indent=2))
        run_load('c-hot-sticky','sticky',duration=120,rps=4,traffic='hot80',records=schedule(120,4,'hot80',chunks=2,chunk_seconds=.05))
    # Intentionally overflow a small bounded queue. Hold a >threshold latency
    # and rejection ratio for longer than both rule windows and hold periods.
    upgrade(**{'backend.queueLimit':4,'backend.queueTimeout':.5,'backend.cpuSeconds':.15})
    ready()
    with observe('c-alerts'),observe_prom('c-alerts'):
        run_load('c-alert-overload',duration=90,rps=20,records=schedule(90,20,chunks=4,chunk_seconds=.2))
        run_load('c-alert-recovery',duration=130,rps=.5,records=schedule(130,.5,chunks=1,chunk_seconds=.01))
    (OUT/'c-hpa-final.json').write_text(json.dumps(get('hpa'),indent=2))
    upgrade(**{'hpa.enabled':False,'replicaCount':2,'backend.queueLimit':64,'backend.queueTimeout':60,'backend.cpuSeconds':.02})
    ready()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('experiment',choices=['clients','a','force','b','c','all'])
    p.add_argument('--duration',type=float,default=120)
    p.add_argument('--rps',type=float,default=4)
    args=p.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    setup_clients()
    if args.experiment in ('a','all'):experiment_a()
    if args.experiment=='force':
        checks=json.loads((OUT/'a-checks.json').read_text()) if (OUT/'a-checks.json').exists() else {}
        checks.update(forced_failure())
        (OUT/'a-checks.json').write_text(json.dumps(checks,indent=2))
        assert all(checks.values()),checks
    if args.experiment in ('b','all'):experiment_b(args.duration,args.rps)
    if args.experiment in ('c','all'):experiment_c()


if __name__=='__main__':main()
