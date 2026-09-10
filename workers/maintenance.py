import logging
import time
from sqlalchemy import select
from packages.domain import storage
from packages.domain.db import now, transaction, uid
from packages.domain.models import ArtifactDeletion, Export, File, ImportJob, Outbox


def schedule_cleanup():
    with transaction(write=True) as s:
        expired=s.scalars(select(Export).where(Export.expires_at<time.time(),Export.object_key.is_not(None))).all()
        for e in expired:
            old=s.scalar(select(ArtifactDeletion).where(ArtifactDeletion.org_id==e.org_id,ArtifactDeletion.export_id==e.id))
            if not old:
                old=ArtifactDeletion(id=uid(),org_id=e.org_id,export_id=e.id);s.add(old);s.flush()
                s.add(Outbox(org_id=e.org_id,event_key='cleanup:'+old.id,kind='cleanup',resource_id=old.id))


def process_cleanup(ident):
    # Object deletion is idempotent. Metadata, hash and snapshot remain in the database.
    with transaction(write=True) as s:
        row=s.scalar(select(ArtifactDeletion).where(ArtifactDeletion.id==ident).with_for_update())
        if not row:return
        event=s.scalar(select(Outbox).where(Outbox.org_id==row.org_id,Outbox.event_key=='cleanup:'+ident))
        if row.deleted_at:event.completed=True;return
        e=s.scalar(select(Export).where(Export.org_id==row.org_id,Export.id==row.export_id).with_for_update())
        if e.expires_at>time.time():return
        row.attempts+=1
        # Global reference check is internal maintenance, never exposed across tenants.
        referenced=s.scalar(select(File.id).where(File.object_key==e.object_key).limit(1)) or s.scalar(select(ImportJob.id).where(ImportJob.result_key==e.object_key).limit(1)) or s.scalar(select(Export.id).where(Export.id!=e.id,Export.object_key==e.object_key,Export.expires_at>time.time()).limit(1))
        if referenced:row.error='REFERENCED_OBJECT';return
        try:
            storage.delete(e.object_key)
            row.deleted_at,row.error=now(),None
            event.completed=True
        except Exception as exc:
            row.error=type(exc).__name__
            logging.getLogger('maintenance').error('cleanup_failed export_id=%s error_type=%s',e.id,row.error)
