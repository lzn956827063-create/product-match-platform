def execute_job(kind, ident):
    from workers.jobs import process_export, process_run
    from workers.imports import process_import
    from workers.maintenance import process_cleanup
    from workers.shadow import process_shadow
    from workers.releases import process_validation, process_files
    from workers.catalog_changes import process_change
    from workers.deliveries import process_delivery
    from workers.annotation_scores import process_scores
    handlers={'match':process_run,'export':process_export,'import':process_import,'cleanup':process_cleanup,'shadow':process_shadow,'release_validate':process_validation,'release_files':process_files,'catalog_change':process_change,'delivery':process_delivery,'annotation_score':process_scores}
    if kind not in handlers:raise ValueError('UNKNOWN_JOB_KIND')
    import time
    from packages.domain.telemetry import increment
    started=time.perf_counter()
    try:return handlers[kind](ident)
    except Exception:
        increment("worker_"+kind+"_errors");raise
    finally:
        increment("worker_"+kind+"_count")
        increment("worker_"+kind+"_seconds",time.perf_counter()-started)
