"""Check retained evidence completeness, without substituting for live K8s tests."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results'
def read(path):return json.loads(path.read_text())
def main():
 findings={}; runs={p.stem.removesuffix('-summary'):read(p) for p in OUT.glob('*-summary.json')}
 expected=[f'b-{traffic}-{policy}-{repeat}' for traffic in ('uniform','hot80') for repeat in range(1,4) for policy in ('sticky','normal')]
 findings['twelve_b_runs']=all(name in runs for name in expected)
 findings['b_measurement_at_least_120s']=all(runs.get(name,{}).get('measurement_seconds',0)>=120 for name in expected)
 findings['b_all_planned_sent_and_accounted']=all((r:=runs.get(name,{})).get('planned',-1)==r.get('sent',-2)==sum(r.get(k,0) for k in ('completed','rejected','aborted','timeouts','transport_errors')) for name in expected)
 findings['identical_schedules_within_each_policy_pair']=all(runs.get(f'b-{traffic}-sticky-{repeat}',{}).get('schedule_sha256','missing1')==runs.get(f'b-{traffic}-normal-{repeat}',{}).get('schedule_sha256','missing2') for traffic in ('uniform','hot80') for repeat in range(1,4))
 findings['a_all_checks']=all(read(OUT/'a-checks.json').values()) if (OUT/'a-checks.json').exists() else False
 clients=read(OUT/'clients.json') if (OUT/'clients.json').exists() else []
 findings['four_distinct_client_ips']=len(clients)==4 and len({c['ip'] for c in clients})==4
 monitor=OUT/'monitoring/checks.json';findings['monitoring_checks']=monitor.exists() and all(read(monitor).values())
 timeline=OUT/'c-scaling-timeline.jsonl'
 sequence=[]
 if timeline.exists():
  for line in timeline.read_text().splitlines():
   t=json.loads(line)
   n=sum(not p.get('deleted') and any(c.get('type')=='Ready' and c.get('status')=='True' for c in p.get('conditions') or []) for p in t.get('pods',[]))
   if not sequence or sequence[-1]!=n:sequence.append(n)
 findings['hpa_ready_2_then_3_then_2']=any(sequence[i]==2 and 3 in sequence[i+1:] and 2 in sequence[sequence.index(3,i+1)+1:] for i in range(len(sequence)) if 3 in sequence[i+1:])
 states=defaultdict(set);normal_seen={};firing_seen={}
 samples=sorted((json.loads(line) for path in OUT.glob('c-*-prometheus.jsonl') for line in path.read_text().splitlines()),key=lambda t:t['unix'])
 for t in samples:
   active={a['labels']['alertname']:a['state'] for a in t.get('alerts',{}).get('data',{}).get('alerts',[])}
   for name in ('HW11HighTTFC','HW11HighRejectionRatio'):
    state=active.get(name,'inactive');states[name].add(state)
    if state=='firing':firing_seen[name]=True
    elif firing_seen.get(name) and state=='inactive':normal_seen[name]=True
 for name in ('HW11HighTTFC','HW11HighRejectionRatio'):
  findings[name+'_pending_firing_recovered']={'pending','firing'}.issubset(states[name]) and normal_seen.get(name,False)
 findings['k8s_checks_retained']=all((OUT/name).exists() for name in ('test-warmup.json','test-rollback.json','test-replacement.json','test-dialogue-isolation.json'))
 routes=read(OUT/'route-analysis.json') if (OUT/'route-analysis.json').exists() else {}
 findings['observed_sticky_without_skew']=bool(routes.get('no_skew_uniform_sticky_cases'))
 findings['hot_queue_and_other_idle_from_prometheus']=bool(routes.get('prometheus_simultaneous_idle',{}).get('hot80_samples'))
 result={'checks':findings,'passed':all(findings.values()),'ready_sequence':sequence,'alert_states':{k:sorted(v) for k,v in states.items()},'b_run_count':sum(name in runs for name in expected)}
 (OUT/'audit.json').write_text(json.dumps(result,indent=2))
 print(json.dumps(result,indent=2))
 return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
