"""Run the built web client, API and durable database worker locally."""
import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=18765);p.add_argument('--seed',action='store_true');a=p.parse_args()
    root=Path(__file__).resolve().parents[1];os.chdir(root)
    if not (root/'apps/web/dist/index.html').exists():raise SystemExit('Build the web app first: cd apps/web && pnpm install && pnpm build')
    env=os.environ.copy();env.setdefault('ALLOWED_ORIGINS',f'http://127.0.0.1:{a.port},http://localhost:{a.port}')
    from packages.domain.db import initialize
    initialize()
    if a.seed:subprocess.run([sys.executable,'-m','scripts.seed'],check=True,env=env)
    processes=[subprocess.Popen([sys.executable,'-m','uvicorn','apps.api.main:app','--host','127.0.0.1','--port',str(a.port)],env=env),subprocess.Popen([sys.executable,'-m','workers.dispatcher'],env=env)]
    def stop(*_):
        for process in processes:
            if process.poll() is None:process.terminate()
    signal.signal(signal.SIGTERM,stop)
    print(f'Open http://127.0.0.1:{a.port}')
    try:processes[0].wait()
    except KeyboardInterrupt:pass
    finally:
        stop()
        for process in processes:
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill()


if __name__=='__main__':main()
