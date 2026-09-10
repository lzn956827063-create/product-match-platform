"""Persistent, atomic delta staging for the local receiver; not an ERP connector."""
import json
from packages.matching.normalize import digest

COLUMNS=('stable_key','source_sku','source_name','status','target_sku','revision_id','run_id','decision_id','decision_actor_id','decision_at','decision_reason','catalog_version_id','policy_id')


def initialize(s):
    s.executescript('CREATE TABLE IF NOT EXISTS delta_stage(event_id TEXT, number INTEGER, value TEXT, PRIMARY KEY(event_id,number)); CREATE TABLE IF NOT EXISTS delta_stage_meta(event_id TEXT PRIMARY KEY, release_id TEXT, base_release_id TEXT, total INTEGER, delta_hash TEXT);')


def stage(s,event_id,page,offset):
    metadata=(page['release_id'],page['base_release_id'],page['total'],page['delta_hash'])
    existing=s.execute('SELECT release_id,base_release_id,total,delta_hash FROM delta_stage_meta WHERE event_id=?',(event_id,)).fetchone()
    if existing and tuple(existing)!=metadata:raise ValueError('DELTA_METADATA_CHANGED')
    if not existing:s.execute('INSERT INTO delta_stage_meta VALUES(?,?,?,?,?)',(event_id,*metadata))
    for number,row in enumerate(page['items'],offset):
        value=json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        old=s.execute('SELECT value FROM delta_stage WHERE event_id=? AND number=?',(event_id,number)).fetchone()
        if old and old[0]!=value:raise ValueError('DELTA_PAGE_CHANGED')
        if not old:s.execute('INSERT INTO delta_stage VALUES(?,?,?)',(event_id,number,value))


def complete(s,event_id):
    meta=s.execute('SELECT release_id,base_release_id,total,delta_hash FROM delta_stage_meta WHERE event_id=?',(event_id,)).fetchone()
    if not meta:raise ValueError('DELTA_INCOMPLETE')
    values=s.execute('SELECT number,value FROM delta_stage WHERE event_id=? ORDER BY number',(event_id,)).fetchall()
    if [x[0] for x in values]!=list(range(meta[2])):raise ValueError('DELTA_INCOMPLETE')
    rows=[json.loads(x[1]) for x in values]
    if digest(rows)!=meta[3]:raise ValueError('DELTA_HASH')
    return meta,rows


def project(row):
    result={k:'' if row.get(k) is None else str(row.get(k)) for k in COLUMNS}
    return {k:"'"+v if v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r')) else v for k,v in result.items()}


def apply(s,event_id,batch_id,release_id,number):
    """Caller wraps this with batch pointer and receipt writes in the same transaction."""
    meta,rows=complete(s,event_id)
    previous=s.execute('SELECT release_id,number,invalidated FROM batches WHERE batch_id=?',(batch_id,)).fetchone()
    if previous and previous[1]>=number:return 'OLDER_OR_DUPLICATE'
    if not previous or previous[0]!=meta[1] or previous[2]:raise ValueError('FULL_SYNC_REQUIRED')
    if meta[0]!=release_id:raise ValueError('DELTA_TARGET')
    for row in rows:
        if row['op']=='delete':s.execute('DELETE FROM mappings WHERE batch_id=? AND stable_key=?',(batch_id,row['stable_key']))
        elif row['op'] in ('upsert','invalidate') and row['current']:
            s.execute('INSERT INTO mappings(batch_id,stable_key,row_data) VALUES(?,?,?) ON CONFLICT(batch_id,stable_key) DO UPDATE SET row_data=excluded.row_data',(batch_id,row['stable_key'],json.dumps(project(row['current']),ensure_ascii=False)))
        else:raise ValueError('DELTA_OPERATION')
    return 'APPLIED'
