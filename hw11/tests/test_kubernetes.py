"""Integration checks call HTTP inside Kubernetes; enable with HW11_K8S_TESTS=1.

These intentionally roll out and replace only the homework release. They do not
substitute mocked readiness, in-memory clients, or saved evidence for Kubernetes.
"""
import json
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import experiments as e


@unittest.skipUnless(os.environ.get('HW11_K8S_TESTS')=='1','set HW11_K8S_TESTS=1 with the HW11 cluster running')
class KubernetesChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        e.OUT.mkdir(exist_ok=True,parents=True)
        e.setup_clients()
        e.upgrade(**{'model.version':'A','model.probeMode':'fixed','model.neverReady':False,'hpa.enabled':False,'replicaCount':2})
        e.ready()

    def payload(self,session='alpha',control='CONTROL_ALPHA'):
        return {'request_id':f'test-{session}-{time.time_ns()}','session_id':session,'prompt':'continue',
                'history':[{'role':'user','content':control}],'max_chunks':1,'chunk_seconds':0}

    def generate(self,payload,url='http://hw11-normal:8000'):
        return [json.loads(line) for line in e.http(e.PODS[0],url+'/generate',payload).splitlines() if line.strip()]

    def test_01_warmup_is_excluded_from_endpoints(self):
        previous={p['metadata']['name'] for p in e.servers()}
        e.k('rollout','restart','deployment/'+e.DEPLOY)
        deadline=time.monotonic()+90
        warming=None
        while time.monotonic()<deadline:
            candidates=[p for p in e.servers() if p['metadata']['name'] not in previous and p['status'].get('podIP')]
            if candidates:
                warming=candidates[0];break
            time.sleep(.5)
        self.assertIsNotNone(warming,'new Pod did not receive an IP')
        name=warming['metadata']['name'];ip=warming['status']['podIP']
        script=('import urllib.request,urllib.error,json,time; '
                f'url="http://{ip}:8000/ready"; '
                '\nfor _ in range(20):\n'
                ' try:\n'
                '  r=urllib.request.urlopen(url,timeout=1);print(json.dumps({"status":r.status,"body":json.load(r)}));break\n'
                ' except urllib.error.HTTPError as ex:\n'
                '  print(json.dumps({"status":ex.code,"body":json.load(ex)}));break\n'
                ' except OSError: time.sleep(.2)\n')
        response=json.loads(e.remote(e.PODS[0],script))
        self.assertEqual(response['status'],503,response)
        self.assertFalse(response['body']['warm'])
        slices=e.get('endpointslices')['items']
        entries=[endpoint for item in slices if item['metadata'].get('labels',{}).get('kubernetes.io/service-name')=='hw11-normal'
                 for endpoint in item.get('endpoints',[]) if ip in endpoint['addresses']]
        self.assertTrue(all(not endpoint['conditions'].get('ready',False) for endpoint in entries))
        e.ready()
        rows=self.generate(self.payload())
        self.assertTrue(rows[-1]['done'])
        (e.OUT/'test-warmup.json').write_text(json.dumps({'pod':name,'response':response,'endpoints':entries},indent=2))

    def test_02_rollback_returns_actual_artifact_a(self):
        e.upgrade(**{'model.version':'B'});e.ready()
        before=self.generate(self.payload())
        self.assertEqual(before[0]['model_version'],'B')
        e.upgrade(**{'model.version':'A'});e.ready()
        after=self.generate(self.payload())
        self.assertEqual(after[0]['model_version'],'A')
        self.assertNotEqual(before[0]['artifact_sha256'],after[0]['artifact_sha256'])
        (e.OUT/'test-rollback.json').write_text(json.dumps({'before':before,'after':after},indent=2))

    def test_03_replaced_replica_restores_version_and_dialogue(self):
        initial=e.servers();initial_names={p['metadata']['name'] for p in initial}
        old=initial[0];old_name=old['metadata']['name'];old_ip=old['status']['podIP']
        payload=self.payload('replacement','CONTROL_PERSISTED_IN_CLIENT_HISTORY')
        before=self.generate(payload,'http://'+old_ip+':8000')
        e.k('delete','pod',old_name,'--wait=false')
        deadline=time.monotonic()+120
        replacement=[]
        while time.monotonic()<deadline:
            replacement=[p for p in e.servers() if p['metadata']['name'] not in initial_names
                         and p['status'].get('podIP') and any(c['type']=='Ready' and c['status']=='True' for c in p['status'].get('conditions',[]))]
            if replacement:break
            time.sleep(1)
        self.assertTrue(replacement,'replacement Pod never became Ready')
        e.ready()
        after=self.generate(payload,'http://'+replacement[-1]['status']['podIP']+':8000')
        self.assertEqual(after[0]['model_version'],before[0]['model_version'])
        self.assertEqual(after[0]['artifact_sha256'],before[0]['artifact_sha256'])
        self.assertIn('CONTROL_PERSISTED_IN_CLIENT_HISTORY',after[0]['text'])
        self.assertNotEqual(after[0]['pod'],old_name)
        (e.OUT/'test-replacement.json').write_text(json.dumps({'before':before,'after':after},indent=2))

    def test_04_two_dialogues_are_isolated_over_real_http(self):
        alpha=self.generate(self.payload('alpha','CONTROL_ALPHA_UNIQUE'))
        beta=self.generate(self.payload('beta','CONTROL_BETA_UNIQUE'))
        alpha_again=self.generate(self.payload('alpha','CONTROL_ALPHA_UNIQUE'))
        self.assertIn('CONTROL_ALPHA_UNIQUE',alpha[0]['text'])
        self.assertNotIn('CONTROL_BETA_UNIQUE',alpha_again[0]['text'])
        self.assertIn('CONTROL_BETA_UNIQUE',beta[0]['text'])
        self.assertNotIn('CONTROL_ALPHA_UNIQUE',beta[0]['text'])
        (e.OUT/'test-dialogue-isolation.json').write_text(json.dumps({'alpha':alpha,'beta':beta,'alpha_again':alpha_again},indent=2))


if __name__=='__main__':unittest.main()
