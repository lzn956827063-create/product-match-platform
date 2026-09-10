def execute_job(kind, ident):
    from workers.jobs import process_export, process_run
    from workers.imports import process_import
    from workers.maintenance import process_cleanup
    from workers.shadow import process_shadow
    handlers={'match':process_run,'export':process_export,'import':process_import,'cleanup':process_cleanup,'shadow':process_shadow}
    if kind not in handlers:raise ValueError('UNKNOWN_JOB_KIND')
    return handlers[kind](ident)
