from sqlalchemy import select,delete,insert
from packages.domain.db import transaction,uid
from packages.domain.models import *
from packages.domain.auth import Context
from packages.domain.quality import revision_quality
from packages.domain.materialized import generation,request_quality


def process_quality(ident):
    with transaction() as s:
        revision=s.get(Revision,ident)
        if not revision:return
        org=revision.org_id;expected=generation(s,revision)
        old=s.scalar(select(QualitySnapshot).where(QualitySnapshot.org_id==org,QualitySnapshot.revision_id==ident))
        report=None if old and old.generation==expected else revision_quality(s,Context(org,revision.created_by,[]),revision)
    with transaction(write=True) as s:
        revision=s.get(Revision,ident);s.scalar(select(Organization).where(Organization.id==org).with_for_update())
        if generation(s,revision)!=expected:request_quality(s,revision);return
        if report is not None:
            issues=report.pop('issues');report['issue_count']=len(issues)
            snapshot=s.scalar(select(QualitySnapshot).where(QualitySnapshot.org_id==org,QualitySnapshot.revision_id==ident))
            if not snapshot:snapshot=QualitySnapshot(id=uid(),org_id=org,revision_id=ident,generation=expected,report=report);s.add(snapshot);s.flush()
            else:snapshot.generation=expected;snapshot.report=report
            s.execute(delete(QualityIssue).where(QualityIssue.org_id==org,QualityIssue.snapshot_id==snapshot.id))
            for start in range(0,len(issues),500):s.execute(insert(QualityIssue),[dict(id=uid(),org_id=org,snapshot_id=snapshot.id,number=n,data=x) for n,x in enumerate(issues[start:start+500],start)])
        for e in s.scalars(select(Outbox).where(Outbox.org_id==org,Outbox.kind=='quality',Outbox.resource_id==ident)):e.completed=True


def schedule_quality():
    # Only small immutable metadata is inspected; source rows/events are worker work.
    with transaction(write=True) as s:
        count=0
        snapshots={x.revision_id:x.generation for x in s.scalars(select(QualitySnapshot))}
        for revision in s.scalars(select(Revision).order_by(Revision.id)):
            if snapshots.get(revision.id)!=generation(s,revision):request_quality(s,revision);count+=1
        return count
