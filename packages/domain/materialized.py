"""Immutable release deltas and explicitly dated quality snapshots."""
from sqlalchemy import select,insert,delete
from .models import *
from .db import uid,now
from .releases import delta_rows,frozen_rows,result_version
from ..matching.normalize import digest
from .errors import require


def delta(s,release):
    return s.scalar(select(ReleaseDelta).where(ReleaseDelta.org_id==release.org_id,ReleaseDelta.release_id==release.id,ReleaseDelta.algorithm=='delta-v1'))


def persist_delta(s,release,values):
    old=delta(s,release)
    if old:return old
    snapshot=ReleaseDelta(id=uid(),org_id=release.org_id,release_id=release.id,base_release_id=release.base_release_id,count=len(values),snapshot_hash=digest(values))
    s.add(snapshot);s.flush()
    for start in range(0,len(values),500):s.execute(insert(ReleaseDeltaRow),[dict(id=uid(),org_id=release.org_id,delta_id=snapshot.id,number=n,data=v) for n,v in enumerate(values[start:start+500],start)])
    return snapshot


def generation(s,revision):return digest([revision.content_hash,result_version(s,revision.org_id,revision.batch_id)])


def request_quality(s,revision):
    key='quality:'+revision.id
    event=s.scalar(select(Outbox).where(Outbox.org_id==revision.org_id,Outbox.event_key==key))
    if event:event.completed=False;event.published_at=None
    else:s.add(Outbox(org_id=revision.org_id,event_key=key,kind='quality',resource_id=revision.id))


def quality_view(s,revision,offset=0,limit=50):
    snapshot=s.scalar(select(QualitySnapshot).where(QualitySnapshot.org_id==revision.org_id,QualitySnapshot.revision_id==revision.id))
    if not snapshot:return {'revision_id':revision.id,'number':revision.number,'created_at':revision.created_at,'input_hash':revision.content_hash,'status':'PENDING','stale':True,'observed_at':None,'issues':[],'total_issues':None,'next_offset':None}
    stale=snapshot.generation!=generation(s,revision)
    values=list(s.scalars(select(QualityIssue.data).where(QualityIssue.org_id==revision.org_id,QualityIssue.snapshot_id==snapshot.id).order_by(QualityIssue.number).offset(offset).limit(limit))) if limit else []
    total=snapshot.report['issue_count']
    return {**snapshot.report,'status':'STALE' if stale else 'READY','stale':stale,'generation':snapshot.generation,'issues':values,'total_issues':total,'next_offset':offset+limit if limit and offset+limit<total else None,'freshness_note':'审核变化由后台刷新；统计截至 observed_at，过期时请等待刷新后使用。'}
