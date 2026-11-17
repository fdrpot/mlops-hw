"""Read-only inventory of this lab; never records kubeconfig or environment secrets."""
import json
from pathlib import Path
import subprocess
import time
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'results'
def run(*args):
 p=subprocess.run(args,capture_output=True,text=True)
 return {'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
def main():
 data={'captured_unix':time.time(),'backend_mode':'simulated','container_profile':'colima-hw11',
       'profile_limits':{'cpu':4,'memory_gib':6,'disk_gib':16},'before_host_free':'disk-before.txt',
       'commands':{}}
 commands=[('docker',['docker','--context','colima-hw11','info','--format','{{json .}}']),
           ('disk',['df','-h',str(ROOT)]),('docker_disk',['docker','--context','colima-hw11','system','df']),
           ('vm_disk',['colima','ssh','--profile','hw11','--','df','-h','/','/var/lib/docker']),
           ('image',['docker','--context','colima-hw11','image','inspect','hw11-api:local','--format','{{json .}}']),
           ('kind',[str(ROOT/'.tools/bin/kind'),'version']),
           ('helm',[str(ROOT/'.tools/bin/helm'),'version','--short']),
           ('colima',['colima','version']),
           ('uv',['uv','--version']),
           ('kubernetes',[str(ROOT/'.tools/bin/kubectl'),'version','-o','json']),
           ('network',[str(ROOT/'.tools/bin/kubectl'),'-n','kube-system','get','cm','kube-proxy','-o','json']),
           ('nodes',[str(ROOT/'.tools/bin/kubectl'),'get','nodes','-o','json']),
           ('pods',[str(ROOT/'.tools/bin/kubectl'),'-n','hw11','get','pods','-o','json'])]
 for name,args in commands:data['commands'][name]=run(*args)
 (OUT/'environment.json').write_text(json.dumps(data,indent=2))
if __name__=='__main__':main()
