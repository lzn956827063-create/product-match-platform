from collections import Counter
from datetime import datetime
from sqlalchemy import select
from .models import *
from .auth import scoped
from .db import now
from .catalog_changes import IDENTITY_FIELDS


def revision_quality(s,c,revision):
    sources=list(s.scalars(select(Source).where(Source.org_id==c.org_id,Source.revision_id==revision.id).order_by(Source.row_no)))
    sku_counts=Counter(x.sku for x in sources if not x.generated_sku)
    issues=[];missing=Counter();duplicate=conflict=0
    for row in sources:
        absent=[f for f in IDENTITY_FIELDS if row.normalized.get(f) in (None,'')]
        dup=not row.generated_sku and sku_counts[row.sku]>1
        bad=any('冲突' in x for x in row.normalized.get('issues',[]))
        missing.update(absent);duplicate+=int(dup);conflict+=int(bad)
        if absent or dup or bad:issues.append({'source_id':row.id,'row_no':row.row_no,'sku':row.sku,'missing':absent,'duplicate':dup,'internal_conflict':bad})
    events=list(s.execute(select(ReviewEvent,Item.source_id).join(Item,(Item.org_id==ReviewEvent.org_id)&(Item.id==ReviewEvent.item_id)).where(Item.org_id==c.org_id,Item.source_id.in_([r.id for r in sources])).order_by(ReviewEvent.created_at,ReviewEvent.id)))
    first={};last={}
    for event,source_id in events:first.setdefault(source_id,event);last[source_id]=event
    eligible=[r for r in sources if not r.excluded]
    finish=sum(first.get(r.id) is not None and first[r.id].action in ('confirm','unmatched') for r in eligible)
    seconds=[max(0,(datetime.fromisoformat(last[r.id].created_at)-datetime.fromisoformat(r.created_at)).total_seconds()) for r in eligible if r.id in last and last[r.id].action in ('confirm','unmatched')]
    lineage=s.scalar(select(RevisionLineage).where(RevisionLineage.org_id==c.org_id,RevisionLineage.revision_id==revision.id))
    children=list(s.scalars(select(RevisionLineage).where(RevisionLineage.org_id==c.org_id,RevisionLineage.parent_revision_id==revision.id)))
    def metric(count,denominator):return {'numerator':count,'denominator':denominator,'rate':count/denominator if denominator else None}
    return {'revision_id':revision.id,'number':revision.number,'input_hash':revision.content_hash,'created_at':revision.created_at,'observed_at':now(),'scope':'correction_subset' if lineage else 'complete_input','parent_revision_id':lineage.parent_revision_id if lineage else None,'source_count':len(sources),'excluded':len(sources)-len(eligible),'missing_fields':{f:metric(missing[f],len(sources)) for f in IDENTITY_FIELDS},'duplicate_source_ids':metric(duplicate,len(sources)),'internal_conflicts':metric(conflict,len(sources)),'first_review_completion':metric(finish,len(eligible)),'correction_rounds':len(children),'completed_processing_seconds':{'total':sum(seconds),'sample_count':len(seconds),'mean':sum(seconds)/len(seconds) if seconds else None},'issues':issues,'note':'来源编号重复率按涉及行数计算；无匹配不计为供应商错误；补数版本仅统计其子集，不与完整输入直接作总体率比较。'}
