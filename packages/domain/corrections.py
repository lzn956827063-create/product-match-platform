import csv
import io
from sqlalchemy import select
from . import storage
from .auth import scoped
from .db import now, uid
from .errors import require
from .ingest import FIELDS
from .models import *
from .services import audit, create_revision, create_run
from ..matching.normalize import digest, normalize


def source_fields(source, revision):
    return {k: str(source.raw.get(v, '')) for k, v in revision.mapping.items() if k in FIELDS}


def save_draft(s, c, run, source, fields, evidence, expected_version):
    c.permit('operator', 'admin')
    require(run.status == 'SUCCEEDED', 409, 'RUN_NOT_READY', '运行完成后才能补充资料')
    require(source.revision_id == run.revision_id and not source.excluded, 404, 'NOT_FOUND', '来源记录不存在')
    require(fields and set(fields) <= set(FIELDS) and all(len(v)<=10000 for v in fields.values()), 422, 'INVALID_FIELDS', '请输入有效的来源字段')
    require(evidence.strip(), 422, 'EVIDENCE_REQUIRED', '请填写补充资料的来源')
    draft = s.scalar(select(CorrectionDraft).where(CorrectionDraft.org_id==c.org_id, CorrectionDraft.run_id==run.id, CorrectionDraft.source_id==source.id, CorrectionDraft.actor_id==c.user_id).with_for_update())
    require(not draft or not draft.submitted_revision_id, 409, 'DRAFT_SUBMITTED', '草稿已提交，请在新的运行中继续修正')
    require(expected_version == (draft.version if draft else 0), 409, 'VERSION_CONFLICT', '补数草稿已更新，请重新加载')
    revision = scoped(s, Revision, run.revision_id, c)
    merged = {**source_fields(source, revision), **fields}
    require(merged.get('name', '').strip(), 422, 'NAME_REQUIRED', '商品名称不能为空')
    if not draft:
        draft = CorrectionDraft(id=uid(), org_id=c.org_id, run_id=run.id, source_id=source.id, actor_id=c.user_id, fields=fields, evidence=evidence.strip())
        s.add(draft)
    else:
        draft.fields, draft.evidence, draft.version, draft.updated_at = fields, evidence.strip(), draft.version+1, now()
    s.flush()
    audit(s, c, 'correction.save', draft.id, {'source_id': source.id, 'fields': list(fields), 'version': draft.version})
    return draft, normalize(merged)


def submit_drafts(s, c, run, draft_ids):
    c.permit('operator', 'admin')
    require(run.status=='SUCCEEDED', 409, 'RUN_NOT_READY', '原运行尚未完成')
    require(len(set(draft_ids))==len(draft_ids), 422, 'DUPLICATE_DRAFTS', '草稿不可重复')
    drafts = s.scalars(select(CorrectionDraft).where(CorrectionDraft.org_id==c.org_id, CorrectionDraft.run_id==run.id, CorrectionDraft.actor_id==c.user_id, CorrectionDraft.id.in_(draft_ids)).order_by(CorrectionDraft.source_id).with_for_update()).all()
    require(len(drafts)==len(draft_ids), 404, 'NOT_FOUND', '草稿不存在或不属于当前操作人')
    require(all(not d.submitted_revision_id for d in drafts), 409, 'DRAFT_SUBMITTED', '存在已提交的草稿')
    parent = scoped(s, Revision, run.revision_id, c)
    batch = scoped(s, Batch, parent.batch_id, c, True)
    sources = {x.id:x for x in s.scalars(select(Source).where(Source.org_id==c.org_id, Source.id.in_([d.source_id for d in drafts])))}
    buf = io.StringIO(newline=''); writer = csv.DictWriter(buf, fieldnames=list(FIELDS));writer.writeheader()
    for d in drafts:
        writer.writerow({**source_fields(sources[d.source_id], parent), **d.fields})
    raw=buf.getvalue().encode('utf-8-sig')
    file = File(id=uid(), org_id=c.org_id, name=f'补数_{len(drafts)}条.csv', object_key=storage.quota_put(c.org_id, raw, 'csv', s=s), sha256=digest(raw), size=len(raw), encoding='utf-8', sheets=['CSV'])
    s.add(file);s.flush()
    revision = create_revision(s, c, batch, {'file_id':file.id,'sheet':'CSV','header_row':1,'mapping':{k:k for k in FIELDS},'exclude_rows':[]}, fingerprint_context={'parent_run_id':run.id,'draft_ids':sorted(draft_ids)})
    added = s.scalars(select(Source).where(Source.org_id==c.org_id, Source.revision_id==revision.id).order_by(Source.row_no)).all()
    links = {}
    for src, draft in zip(added, drafts):
        links[src.id] = {'parent_source_id':draft.source_id,'parent_row_no':sources[draft.source_id].row_no,'fields':draft.fields,'evidence':draft.evidence,'actor_id':c.user_id,'at':now()}
        draft.submitted_revision_id = revision.id
    s.add(RevisionLineage(org_id=c.org_id, revision_id=revision.id, parent_revision_id=parent.id, parent_run_id=run.id, source_links=links))
    result = create_run(s, c, {'revision_id':revision.id,'catalog_version_id':run.catalog_version_id,'policy_id':run.policy_id})
    audit(s, c, 'correction.submit', revision.id, {'parent_run_id':run.id,'draft_ids':draft_ids,'affected':len(drafts)})
    return {**result,'revision_id':revision.id,'affected':len(drafts),'scope':'correction_subset','parent_run_id':run.id}
