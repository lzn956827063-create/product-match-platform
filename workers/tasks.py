from celery import Celery
from packages.domain.config import REDIS_URL
from workers.routing import execute_job

app = Celery("product_match", broker=REDIS_URL)
app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True, broker_transport_options={"visibility_timeout": 180}, task_ignore_result=True)


@app.task(name="product_match.execute")
def execute(kind, resource_id):
    execute_job(kind, resource_id)
