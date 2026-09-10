"""Explicit full-baseline resolution and immutable, version-checked releases."""
from collections import Counter, defaultdict
from sqlalchemy import delete, insert, select, func
from .models import *
from .auth import Context, scoped
from .db import now, uid
from .errors import require
from .services import audit
from ..matching.normalize import digest

FINAL = {'CONFIRMED', 'UNMATCHED', 'EXCLUDED'}


def batch_state(s, org_id, batch_id):
    state = s.scalar(select(BatchResultState).where(BatchResultState.org_id==org_id, BatchResultState.batch_id==batch_id).with_for_update())
    if state is None:
        state = BatchResultState(org_id=org_id, batch_id=batch_id, version=0)
        s.add(state); s.flush()
    return state


def touch_batch(s, org_id, batch_id):
    state = batch_state(s, org_id, batch_id)
    state.version += 1
    return state.version


def result_version(s, org_id, batch_id):
    return s.scalar(select(BatchResultState.version).where(BatchResultState.org_id==org_id, BatchResultState.batch_id==batch_id)) or 0


def release_event(s, release, kind, actor_id=None, detail=None):
    event = ReleaseEvent(id=uid(), org_id=release.org_id, release_id=release.id, kind=kind, actor_id=actor_id, detail=detail or {})
    s.add(event); s.flush()
    return event


def invalidate_for_item(s, c, item_id, event_id):
    releases = s.scalars(select(BatchRelease).where(BatchRelease.org_id==c.org_id, BatchRelease.status.in_(['PUBLISHED','SUPERSEDED']), BatchRelease.invalidated.is_(False), BatchRelease.id.in_(select(ReleaseRow.release_id).where(ReleaseRow.org_id==c.org_id, ReleaseRow.item_id==item_id)))).all()
    for release in releases:
        release.invalidated = True
        event = release_event(s, release, 'INVALIDATED', c.user_id, {'item_id':item_id,'new_decision_id':event_id})
        from .integrations import enqueue_deliveries
        enqueue_deliveries(s, release, event)


def resolve(s, c, batch_id, config):
    batch = scoped(s, Batch, batch_id, c)
    baseline = scoped(s, Run, config['baseline_run_id'], c)
    rev = scoped(s, Revision, baseline.revision_id, c)
    require(rev.batch_id==batch_id, 422, 'BASELINE_BATCH', '请选择本批次完整输入')
    require(not s.scalar(select(RevisionLineage.id).where(RevisionLineage.org_id==c.org_id, RevisionLineage.revision_id==rev.id)), 422, 'SUBSET_BASELINE', '补数子集不能作为整批完整基线')
    require(baseline.status=='SUCCEEDED', 409, 'BASELINE_NOT_READY', '完整基线运行尚未成功')
    revisions = {r.id:r for r in s.scalars(select(Revision).where(Revision.org_id==c.org_id, Revision.batch_id==batch_id))}
    lineages = list(s.scalars(select(RevisionLineage).where(RevisionLineage.org_id==c.org_id, RevisionLineage.revision_id.in_(revisions))))
    descendants = {rev.id}; pending = list(lineages); selected_lineages=[]
    while pending:
        reachable=[l for l in pending if l.parent_revision_id in descendants]
        if not reachable: break
        for lineage in reachable:
            descendants.add(lineage.revision_id); selected_lineages.append(lineage); pending.remove(lineage)
    sources = {x.id:x for x in s.scalars(select(Source).where(Source.org_id==c.org_id, Source.revision_id.in_(descendants)))}
    roots = sorted((x for x in sources.values() if x.revision_id==rev.id),key=lambda x:x.row_no)
    children=defaultdict(list)
    for lineage in selected_lineages:
        for child, link in lineage.source_links.items():
            require(child in sources and link['parent_source_id'] in sources,409,'LINEAGE_BROKEN','补数来源链接不完整')
            children[link['parent_source_id']].append(child)
    runs_by_revision=defaultdict(list)
    for r in s.scalars(select(Run).where(Run.org_id==c.org_id, Run.revision_id.in_(descendants))): runs_by_revision[r.revision_id].append(r)
    chosen={rev.id:baseline}; blockers=[]
    requested=set(config.get('correction_run_ids',[]))
    known={r.id for values in runs_by_revision.values() for r in values}
    require(requested<=known and baseline.id not in requested,422,'INVALID_BRANCH_RUN','补数运行不属于当前完整基线')
    for rid in descendants-{rev.id}:
        options=runs_by_revision[rid]; explicit=[r for r in options if r.id in requested]
        if len(explicit)==1: chosen[rid]=explicit[0]
        elif len(options)==1: chosen[rid]=options[0]
    chosen_ids=[r.id for r in chosen.values()]
    items={x.source_id:x for x in s.scalars(select(Item).where(Item.org_id==c.org_id, Item.run_id.in_(chosen_ids)))}
    events={e.id:e for e in s.scalars(select(ReviewEvent).where(ReviewEvent.org_id==c.org_id, ReviewEvent.id.in_([i.current_decision_id for i in items.values() if i.current_decision_id])))}
    products={p.id:p for p in s.scalars(select(Product).where(Product.org_id==c.org_id,Product.id.in_([e.product_id for e in events.values() if e.product_id])))}
    from .catalog_changes import release_problem_map
    product_problems=release_problem_map(s,c,products.values())
    overrides={x.source_id:x.stable_key for x in s.scalars(select(SourceIdentity).where(SourceIdentity.org_id==c.org_id, SourceIdentity.batch_id==batch_id))}
    sku_counts=Counter(x.sku for x in roots)
    def leaves(ident, seen=None):
        seen=set() if seen is None else seen
        require(ident not in seen,409,'LINEAGE_CYCLE','补数来源关系存在循环')
        if not children[ident]: return [ident]
        return [leaf for child in children[ident] for leaf in leaves(child,seen|{ident})]
    rows=[]; used=set(); choices=config.get('branch_choices',{})
    require(set(choices)<={r.id for r in roots},422,'INVALID_BRANCH_CHOICE','分支选择必须引用当前完整基线记录')
    for root in roots:
        problems=[]; leaf_ids=leaves(root.id); choice=choices.get(root.id)
        if len(leaf_ids)>1:
            if not choice or choice.get('source_id') not in leaf_ids or not choice.get('reason','').strip():
                problems.append({'code':'BRANCH_CONFLICT','message':'有多个未合并补数分支，请明确选择并说明原因','choices':leaf_ids})
                leaf_id=root.id  # display the unresolved root; never a publishable fallback
            else: leaf_id=choice['source_id']
        else:
            leaf_id=leaf_ids[0]
            require(not choice or choice.get('source_id')==leaf_id,422,'INVALID_BRANCH_CHOICE','选择的补数分支已不是末端记录')
        source=sources[leaf_id]; run=chosen.get(source.revision_id); item=items.get(leaf_id)
        stable=overrides.get(root.id) or ('sku:'+root.sku if not root.generated_sku and sku_counts[root.sku]==1 else None)
        if not stable: problems.append({'code':'AMBIGUOUS_IDENTITY','message':'来源编号缺失或重复，请人工指定稳定业务身份'})
        if stable in used: problems.append({'code':'DUPLICATE_IDENTITY','message':'稳定来源身份重复，请消歧'})
        used.add(stable)
        event=events.get(item.current_decision_id) if item else None
        product=products.get(event.product_id) if event else None
        status='EXCLUDED' if source.excluded else item.status if item else 'PENDING'
        if not source.excluded:
            if not run: problems.append({'code':'RUN_SELECTION_REQUIRED','message':'此修正有多个运行或尚无运行，请明确选定','revision_id':source.revision_id})
            elif run.status!='SUCCEEDED': problems.append({'code':'CORRECTION_NOT_READY','message':'选定的补数运行尚未成功','run_id':run.id})
            if status not in FINAL or not event: problems.append({'code':'UNRESOLVED_DECISION','message':'存在未处理、待补资料或已撤销的决定'})
            if status=='CONFIRMED' and not product: problems.append({'code':'MISSING_PRODUCT','message':'审核商品引用缺失'})
        row={'stable_key':stable or 'unresolved:'+root.id,'root_source_id':root.id,'source_id':source.id,'source_sku':source.sku,'source_name':source.normalized.get('name',''), 'source_fields':source.normalized,'source_raw':source.raw,'revision_id':source.revision_id,'run_id':run.id if run else None,'item_id':item.id if item else None,'item_version':item.version if item else None,'decision_id':event.id if event else None,'decision_actor_id':event.actor_id if event else None,'decision_at':event.created_at if event else None,'decision_reason':event.reason if event else '导入时明确排除','status':status,'product_id':product.id if product else None,'target_sku':product.sku if product else None,'target_fields':product.normalized if product else None,'catalog_version_id':run.catalog_version_id if run else baseline.catalog_version_id,'policy_id':run.policy_id if run else baseline.policy_id,'corrected':source.id!=root.id}
        if product:
            issue=product_problems.get(product.id)
            if issue: problems.append(issue)
        if problems: blockers.append({'root_source_id':root.id,'source_id':source.id,'sku':root.sku,'row_no':root.row_no,'run_id':run.id if run else None,'item_id':item.id if item else None,'status':status,'problems':problems})
        rows.append(row)
    drafts=s.scalar(select(func.count()).select_from(CorrectionDraft).where(CorrectionDraft.org_id==c.org_id, CorrectionDraft.run_id.in_(known), CorrectionDraft.submitted_revision_id.is_(None)))
    supplier=s.scalar(select(BatchSupplier).where(BatchSupplier.org_id==c.org_id,BatchSupplier.batch_id==batch_id))
    return rows, blockers, {'supplier_name':batch.supplier,'supplier_id':supplier.supplier_id if supplier else None,'personal_unsubmitted_drafts':drafts,'baseline_revision_id':rev.id,'resolved_runs':chosen_ids,'counts':dict(Counter(r['status'] for r in rows)), 'scope':'完整基线及其已提交补数分支'}


def delta_rows(old, rows):
    before={r['stable_key']:r for r in old}; after={r['stable_key']:r for r in rows}; delta=[]
    for k in sorted(before.keys() | after.keys()):
        left,right=before.get(k),after.get(k)
        if right is None: delta.append({'op':'delete','stable_key':k,'previous':left,'current':None})
        elif left is None or digest(left)!=digest(right):
            delta.append({'op':'invalidate' if left and left['status']=='CONFIRMED' and right['status']!='CONFIRMED' else 'upsert','stable_key':k,'previous':left,'current':right})
    return delta


def frozen_rows(s, release):
    return [r.data for r in s.scalars(select(ReleaseRow).where(ReleaseRow.org_id==release.org_id, ReleaseRow.release_id==release.id).order_by(ReleaseRow.stable_key))]


def create_release(s,c,batch_id,config):
    c.permit('publisher','operator')
    scoped(s,Batch,batch_id,c,True)
    baseline=scoped(s,Run,config['baseline_run_id'],c)
    revision=scoped(s,Revision,baseline.revision_id,c)
    require(revision.batch_id==batch_id and not s.scalar(select(RevisionLineage.id).where(RevisionLineage.org_id==c.org_id, RevisionLineage.revision_id==revision.id)),422,'SUBSET_BASELINE','请选择本批次完整输入作为基线')
    base_id=config.get('base_release_id')
    if base_id:
        base=scoped(s,BatchRelease,base_id,c)
        require(base.batch_id==batch_id and base.status in ('PUBLISHED','SUPERSEDED','REVOKED'),422,'INVALID_RELEASE_BASE','请选择本批次已发布过的基线版本')
    number=(s.scalar(select(func.max(BatchRelease.number)).where(BatchRelease.org_id==c.org_id,BatchRelease.batch_id==batch_id)) or 0)+1
    r=BatchRelease(id=uid(),org_id=c.org_id,batch_id=batch_id,baseline_run_id=baseline.id,base_release_id=base_id,number=number,config=config,created_by=c.user_id)
    s.add(r);s.flush();batch_state(s,c.org_id,batch_id);release_event(s,r,'CREATED',c.user_id,{'config':config});audit(s,c,'release.create',r.id)
    return r


def queue_validation(s,c,r):
    c.permit('publisher','operator')
    require(r.status in ('DRAFT','READY'),409,'RELEASE_IMMUTABLE','已发布版本不可修改；校验中的草稿请稍后重试')
    r.status='VALIDATING';r.version+=1;r.validation={};r.snapshot_hash=None
    s.add(Outbox(org_id=c.org_id,event_key=f'release_validate:{r.id}:{r.version}',kind='release_validate',resource_id=r.id))
    release_event(s,r,'VALIDATING',c.user_id,{'version':r.version})


def publish_release(s,c,r,expected_version):
    c.permit('publisher')
    require(r.status=='READY' and r.version==expected_version,409,'RELEASE_NOT_READY','请校验当前发布草稿')
    require(r.result_version==batch_state(s,c.org_id,r.batch_id).version,409,'RESULT_CHANGED','校验后结果已变化，请重新校验')
    previous=s.scalar(select(BatchRelease).where(BatchRelease.org_id==c.org_id,BatchRelease.batch_id==r.batch_id,BatchRelease.status=='PUBLISHED').with_for_update())
    require(not previous or r.base_release_id==previous.id,409,'RELEASE_BASE_CHANGED','当前发布版本已变化，请重新创建并比较草稿')
    if previous:
        previous.status='SUPERSEDED';release_event(s,previous,'SUPERSEDED',c.user_id,{'replacement_id':r.id})
    r.status='PUBLISHED';r.published_at=now();r.version+=1
    release_event(s,r,'PUBLISHED',c.user_id,{'snapshot_hash':r.snapshot_hash})
    for kind in ('full','mapped','delta'):
        s.add(ReleaseArtifact(org_id=c.org_id,release_id=r.id,kind=kind))
    s.add(Outbox(org_id=c.org_id,event_key=f'release_files:{r.id}',kind='release_files',resource_id=r.id))
    audit(s,c,'release.publish',r.id,{'snapshot_hash':r.snapshot_hash})
    return r
