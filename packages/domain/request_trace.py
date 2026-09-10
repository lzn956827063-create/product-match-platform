"""Request-local timings; never logs SQL parameters, names or file contents."""
from contextvars import ContextVar
from time import perf_counter
from hashlib import sha256
from starlette.responses import JSONResponse
from sqlalchemy import event

current=ContextVar('request_trace',default=None)


def start(ident):
    return current.set({'trace_id':ident,'sql_count':0,'sql_seconds':0.,'connection_acquire_seconds':0.,'lock_statement_seconds':0.,'json_seconds':0.,'slow_sql':[]})


def add(name,value):
    trace=current.get()
    if trace is not None:trace[name]=trace.get(name,0)+value


class MeasuredJSONResponse(JSONResponse):
    def render(self,content):
        started=perf_counter();result=super().render(content);add('json_seconds',perf_counter()-started);return result


def install(engine):
    @event.listens_for(engine,'before_cursor_execute')
    def before(conn,cursor,statement,parameters,context,many):context._trace_started=perf_counter()
    @event.listens_for(engine,'after_cursor_execute')
    def after(conn,cursor,statement,parameters,context,many):
        trace=current.get()
        if trace is None:return
        elapsed=perf_counter()-context._trace_started;add('sql_seconds',elapsed);add('sql_count',1)
        if 'FOR UPDATE' in statement.upper() or statement.upper().startswith('BEGIN IMMEDIATE'):add('lock_statement_seconds',elapsed)
        if elapsed>=.025 and len(trace['slow_sql'])<10:trace['slow_sql'].append({'shape_hash':sha256(statement.encode()).hexdigest()[:16],'seconds':round(elapsed,6),'many':many})
