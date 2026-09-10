from celery import Celery
from packages.domain.config import REDIS_URL
from workers.jobs import process_export, process_run

app = Celery("product_match", broker=REDIS_URL)
app.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1, broker_connection_retry_on_startup=True, broker_transport_options={"visibility_timeout": 180}, task_ignore_result=True)


@app.task(name="product_match.execute")
def execute(kind, resource_id):
    (process_run if kind == "match" else process_export)(resource_id)
