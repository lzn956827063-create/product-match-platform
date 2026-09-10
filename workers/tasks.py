from celery import Celery
from packages.domain.config import REDIS_URL
from workers.routing import execute_job

app = Celery("product_match", broker=REDIS_URL)
app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True, broker_transport_options={"visibility_timeout": 180}, task_ignore_result=True)


@app.task(name="product_match.execute")
def execute(kind, resource_id):
    execute_job(kind, resource_id)


from celery.signals import worker_process_init
@worker_process_init.connect
def start_metrics(**kwargs):
    # Each prefork child exports its own process counters and gets its own port.
    import os
    from billiard import current_process
    from packages.domain.monitoring import start_server
    if os.getenv('WORKER_METRICS_PORT'):
        os.environ['WORKER_METRICS_PORT']=str(int(os.environ['WORKER_METRICS_PORT'])+getattr(current_process(),'index',0))
        start_server()
