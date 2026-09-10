"""Tenant-scoped bulk serializers. SQL count does not grow with page length."""
from collections import defaultdict
from sqlalchemy import select, func
from sqlalchemy.orm import aliased
from .models import *
from ..matching.normalize import REQUIRED


def data(obj, exclude=()):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns if c.name not in {'org_id', *exclude}}


def item_page(s, c, items):
    if not items:
        return []
    ids = [i.id for i in items]
    sources = {r.id: data(r) for r in s.scalars(select(Source).where(Source.org_id == c.org_id, Source.id.in_([i.source_id for i in items])))}
    lineages = s.scalars(select(RevisionLineage).where(RevisionLineage.org_id == c.org_id, RevisionLineage.revision_id.in_({v['revision_id'] for v in sources.values()}))).all()
    for lineage in lineages:
        for src_id, provenance in lineage.source_links.items():
            if src_id in sources:sources[src_id]['correction'] = provenance
    candidates = defaultdict(list)
    rows = s.execute(select(Candidate, Product).join(Product, (Product.org_id == Candidate.org_id) & (Product.id == Candidate.product_id)).where(Candidate.org_id == c.org_id, Candidate.item_id.in_(ids), Candidate.rank <= 5).order_by(Candidate.rank))
    for cand, product in rows:
        candidates[cand.item_id].append({**data(cand), 'product': data(product)})
    events = {e.id: data(e) for e in s.scalars(select(ReviewEvent).where(ReviewEvent.org_id == c.org_id, ReviewEvent.id.in_([i.current_decision_id for i in items if i.current_decision_id])))}
    return [{**data(i), 'source': sources[i.source_id], 'candidates': candidates[i.id], 'decision': events.get(i.current_decision_id)} for i in items]


def run_page(s, c, runs):
    if not runs:
        return []
    linked = s.execute(select(Revision, Batch, User.name).join(Batch, (Batch.org_id == Revision.org_id) & (Batch.id == Revision.batch_id)).join(Run, (Run.org_id == Revision.org_id) & (Run.revision_id == Revision.id)).join(User, User.id == Run.created_by).where(Run.org_id == c.org_id, Run.id.in_([r.id for r in runs])).add_columns(Run.id))
    metadata = {rid: {'batch_name': b.name, 'batch_id': b.id, 'supplier': b.supplier, 'revision_number': rev.number, 'creator_name': name} for rev, b, name, rid in linked}
    suggestions, decisions = defaultdict(dict), defaultdict(dict)
    for rid, sug, status, count in s.execute(select(Item.run_id, Item.suggestion, Item.status, func.count()).where(Item.org_id == c.org_id, Item.run_id.in_([r.id for r in runs])).group_by(Item.run_id, Item.suggestion, Item.status)):
        suggestions[rid][sug] = suggestions[rid].get(sug, 0) + count
        decisions[rid][status] = decisions[rid].get(status, 0) + count
    return [{**data(r), **metadata[r.id], 'suggestions': suggestions[r.id], 'decisions': decisions[r.id]} for r in runs]


def export_page(s, c, exports):
    ids = [e.id for e in exports]
    stale = dict(s.execute(select(ExportRow.export_id, func.count()).join(Item, (Item.org_id == ExportRow.org_id) & (Item.id == ExportRow.item_id)).where(ExportRow.org_id == c.org_id, ExportRow.export_id.in_(ids), Item.version != ExportRow.item_version).group_by(ExportRow.export_id)).all())
    authors = dict(s.execute(select(Export.id, User.name).join(User, User.id == Export.created_by).where(Export.org_id == c.org_id, Export.id.in_(ids))).all())
    return [{**data(e, ('object_key',)), 'stale': bool(stale.get(e.id)), 'changed_decisions': stale.get(e.id, 0), 'creator_name': authors[e.id]} for e in exports]


def compare_versions(s, c, left, right, offset=0, limit=50):
    """Numbering and source IDs are scoped to this batch, never the organization."""
    chosen = aliased(Product)
    indexed = {left.id: defaultdict(list), right.id: defaultdict(list)}
    q = select(Item, Source, Product.sku, ReviewEvent, chosen.sku).join(Source, (Source.org_id == Item.org_id) & (Source.id == Item.source_id)).outerjoin(Candidate, (Candidate.org_id == Item.org_id) & (Candidate.item_id == Item.id) & (Candidate.rank == 1)).outerjoin(Product, (Product.org_id == Candidate.org_id) & (Product.id == Candidate.product_id)).outerjoin(ReviewEvent, (ReviewEvent.org_id == Item.org_id) & (ReviewEvent.id == Item.current_decision_id)).outerjoin(chosen, (chosen.org_id == ReviewEvent.org_id) & (chosen.id == ReviewEvent.product_id)).where(Item.org_id == c.org_id, Item.run_id.in_([left.id, right.id]))
    lineage_rows = s.scalars(select(RevisionLineage).where(RevisionLineage.org_id==c.org_id,RevisionLineage.revision_id.in_([left.revision_id,right.revision_id]))).all()
    restrictions = {l.parent_run_id:{v['parent_source_id'] for v in l.source_links.values()} for l in lineage_rows if l.parent_run_id in (left.id,right.id)}
    for i, src, best, ev, decided in s.execute(q):
        if i.run_id in restrictions and src.id not in restrictions[i.run_id]:continue
        indexed[i.run_id][src.sku].append({'row_no': src.row_no, 'source_id': src.id, 'generated': src.generated_sku, 'suggestion': i.suggestion, 'status': i.status, 'target_sku': best, 'decision_sku': decided, 'reason': ev.reason if ev else '', 'fields': {k: v for k, v in src.normalized.items() if k not in ('provenance', 'issues', 'missing', 'search_text')}, 'raw': src.raw})
    a, b = indexed[left.id], indexed[right.id]
    changes, ambiguous = [], []
    for sku in sorted(set(a) | set(b)):
        aa, bb = a[sku], b[sku]
        if len(aa) > 1 or len(bb) > 1 or any(x['generated'] for x in aa+bb):
            ambiguous.append({'sku': sku, 'reason': '重复来源编号' if len(aa)>1 or len(bb)>1 else '缺少稳定来源编号（系统行号不可跨文件匹配）', 'before_rows': [x['row_no'] for x in aa], 'after_rows': [x['row_no'] for x in bb]})
            continue
        before, after = aa[0] if aa else None, bb[0] if bb else None
        types, fields = [], []
        if before is None:
            types.append('added')
        elif after is None:
            types.append('removed')
        else:
            fields = [f for f in set(before['fields']) | set(after['fields']) if before['fields'].get(f) != after['fields'].get(f)]
            if fields or before['raw'] != after['raw']:
                types.append('fields')
            if any(f in REQUIRED for f in fields):
                types.append('identity_fields')
            if (before['suggestion'], before['target_sku']) != (after['suggestion'], after['target_sku']):
                types.append('candidate')
            if (before['status'], before['decision_sku'], before['reason']) != (after['status'], after['decision_sku'], after['reason']):
                types.append('decision')
        if types:
            changes.append({'sku': sku, 'row_no': (after or before)['row_no'], 'types': types, 'changed_fields': sorted(fields), 'before': before, 'after': after})
    return {'changes': changes[offset:offset+limit], 'total': len(changes), 'next_cursor': str(offset+limit) if offset+limit<len(changes) else None, 'ambiguities': ambiguous[offset:offset+limit], 'ambiguity_total': len(ambiguous), 'ambiguity_next_cursor': str(offset+limit) if offset+limit<len(ambiguous) else None, 'identity': '同一组织、供应商批次内唯一来源编号；歧义不自动对应', 'manifest_before': left.manifest, 'manifest_after': right.manifest}


def item_summaries(s, c, items):
    if not items:return []
    query=select(Item.id,Source.id,Source.row_no,Source.sku,Source.generated_sku,Source.normalized['name'].as_string(),Candidate.product_id,Candidate.score).join(Source,(Source.org_id==Item.org_id)&(Source.id==Item.source_id)).outerjoin(Candidate,(Candidate.org_id==Item.org_id)&(Candidate.item_id==Item.id)&(Candidate.rank==1)).where(Item.org_id==c.org_id,Item.id.in_([i.id for i in items]))
    linked={ident:{'source':{'id':source_id,'row_no':row_no,'sku':sku,'generated_sku':generated,'normalized':{'name':name}},'candidates':[{'product_id':product,'score':score,'rank':1}] if product else [],'decision':None,'summary_only':True} for ident,source_id,row_no,sku,generated,name,product,score in s.execute(query)}
    return [{**data(item),**linked[item.id]} for item in items]
