"""Low-cardinality process metrics; durable queue state is read from the database."""
from collections import defaultdict
from threading import Lock

_lock=Lock();_counts=defaultdict(int);_sums=defaultdict(float)
BOUNDS=(.01,.025,.05,.1,.25,.5,1,2,5,10,float('inf'))
_hist=defaultdict(lambda:[0]*len(BOUNDS))


def increment(name, amount=1):
    with _lock:_counts[name]+=amount


def observe(route,method,status,seconds):
    # Only FastAPI route templates, never user-supplied paths or IDs.
    label=(route,method,str(status))
    with _lock:
        _counts[('http',*label)]+=1;_sums[label]+=seconds
        for i,upper in enumerate(BOUNDS):
            if seconds<=upper:_hist[label][i]+=1


def render():
    with _lock:
        lines=[f'product_match_{k} {v}' for k,v in _counts.items() if isinstance(k,str)]
        for (route,method,status),buckets in _hist.items():
            labels=f'route="{route}",method="{method}",status="{status}"'
            for upper,count in zip(BOUNDS,buckets):lines.append(f'product_match_http_seconds_bucket{{{labels},le="{upper if upper!=float("inf") else "+Inf"}"}} {count}')
            lines.append(f'product_match_http_seconds_count{{{labels}}} {_counts[("http",route,method,status)]}')
            lines.append(f'product_match_http_seconds_sum{{{labels}}} {_sums[(route,method,status)]}')
        return lines
