"""Record the actual host and container-runtime availability without changing it."""
import json,platform,shutil,subprocess,os
from pathlib import Path


def main():
    docker=shutil.which('docker');available=False;reason='Docker executable unavailable'
    if docker:
        r=subprocess.run([docker,'info','--format','{{.ServerVersion}}'],capture_output=True,text=True,timeout=20);available=r.returncode==0;reason=r.stdout.strip() if available else 'Docker engine unavailable'
    report={'platform':platform.platform(),'cpu_count':os.cpu_count(),'docker_executable':docker,'docker_engine_available':available,'reason':reason,'production_stack_executed':False,'not_verified':['PostgreSQL Redis Celery MinIO reverse proxy integrated deployment','4 CPU 16 GiB production stack 30-minute capacity and total worker memory','Real PostgreSQL and MinIO outage timeline','Prometheus Grafana deployed alert-to-recovery exercise','Production RPO 24h RTO 2h and monthly availability'],'local_evidence_is_separate':True}
    Path('docs/enterprise/environment.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))


if __name__=='__main__':main()
