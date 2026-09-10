"""Tamper-evident return templates, version checks and draft-only atomic import."""
import base64,hashlib,hmac,json
from sqlalchemy import select
from .models import *
from .config import JWT_SECRET
from .auth import scoped
from .errors import require
from .ingest import FIELDS,parse_file
from .corrections import source_fields,save_draft
from .services import idempotent,audit
from ..matching.normalize import digest
from workers.releases import csv_content

FIXED=('problem_id','source_id','run_id','revision_id','item_version','draft_version','verification')


def sign(payload):
    value=base64.urlsafe_b64encode(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).decode()
    return value+'.'+hmac.new(JWT_SECRET.encode(),('correction-return-v1:'+value).encode(),hashlib.sha256).hexdigest()


def verify(token):
    try:
        value,signature=token.split('.')
        expected=hmac.new(JWT_SECRET.encode(),('correction-return-v1:'+value).encode(),hashlib.sha256).hexdigest()
        require(hmac.compare_digest(signature,expected),422,'TEMPLATE_SIGNATURE','模板固定信息已被修改，请重新下载')
        return json.loads(base64.urlsafe_b64decode(value))
    except (ValueError,KeyError):
        require(False,422,'TEMPLATE_SIGNATURE','模板校验信息无效')


def template(s,c,run):
    c.permit('operator','admin');revision=scoped(s,Revision,run.revision_id,c)
    drafts={d.source_id:d for d in s.scalars(select(CorrectionDraft).where(CorrectionDraft.org_id==c.org_id,CorrectionDraft.run_id==run.id,CorrectionDraft.actor_id==c.user_id))}
    rows=[]
    query=select(Item,Source).join(Source,(Source.org_id==Item.org_id)&(Source.id==Item.source_id)).where(Item.org_id==c.org_id,Item.run_id==run.id,(Item.status=='NEEDS_INFO')|Item.suggestion.in_(['CONFLICT','REVIEW','NO_CANDIDATE'])).order_by(Source.row_no)
    for item,src in s.execute(query):
        d=drafts.get(src.id)
        if src.excluded or (d and d.submitted_revision_id):continue
        fixed={'problem_id':item.id,'source_id':src.id,'run_id':run.id,'revision_id':revision.id,'item_version':str(item.version),'draft_version':str(d.version if d else 0)}
        proof={**fixed,'org_id':c.org_id,'actor_id':c.user_id,'input_hash':revision.content_hash}
        rows.append({**source_fields(src,revision),**(d.fields if d else {}),**fixed,'verification':sign(proof),'原始行号':src.row_no,'问题原因':item.status+' / '+item.suggestion,'资料来源':d.evidence if d else ''})
    return csv_content(rows,[*FIXED,'原始行号',*FIELDS,'问题原因','资料来源'])


def import_return(s,c,run,raw,filename):
    c.permit('operator','admin');parsed=parse_file(raw,filename)
    require(len(parsed)==1,422,'RETURN_SHEET','回填文件必须只有一个工作表')
    sheet=next(iter(parsed.values()));rows=sheet['rows']
    require(not sheet['formulas'] and 1<len(rows)<=1001,422,'RETURN_SIZE','请回传 1 至 1000 条普通值记录，不支持公式')
    headers=rows[0];require(len(headers)==len(set(headers)) and set(FIXED)|set(FIELDS)|{'资料来源'}<=set(headers),422,'RETURN_HEADERS','请使用新版问题模板，保留所有固定列和字段列')
    entries=[dict(zip(headers,r)) for r in rows[1:] if any(v.strip() for v in r)]
    require(entries and len({r.get('problem_id') for r in entries})==len(entries),422,'DUPLICATE_PROBLEMS','文件包含重复或空问题编号')
    fingerprint=digest(sorted(entries,key=lambda x:x.get('problem_id','')))
    def apply():
        revision=scoped(s,Revision,run.revision_id,c);validated=[]
        for row in entries:
            proof=verify(row.get('verification',''))
            require(all(str(proof.get(k))==row.get(k) for k in FIXED if k!='verification') and proof.get('org_id')==c.org_id and proof.get('actor_id')==c.user_id and proof.get('run_id')==run.id and proof.get('revision_id')==revision.id and proof.get('input_hash')==revision.content_hash,422,'RETURN_IDENTITY','回填模板不属于当前操作人、运行或来源')
            item=scoped(s,Item,row['problem_id'],c);source=scoped(s,Source,row['source_id'],c)
            require(item.run_id==run.id and item.source_id==source.id and source.revision_id==revision.id,422,'RETURN_IDENTITY','问题与来源对应关系不一致')
            require(item.version==int(proof['item_version']),409,'STALE_TEMPLATE','原问题已重新审核，请下载最新模板')
            original=source_fields(source,revision)
            # Strip only the export escape for an unchanged dangerous value.
            values={k:original.get(k,'') if row.get(k,'')=="'"+original.get(k,'') else row.get(k,'') for k in FIELDS}
            fields={k:v for k,v in values.items() if v!=original.get(k,'')}
            require(fields and row.get('资料来源','').strip(),422,'RETURN_EVIDENCE','每条回填须有实际字段修正和资料依据')
            validated.append((source,fields,row['资料来源'],int(proof['draft_version'])))
        ids=[]
        for source,fields,evidence,version in validated:
            d,_=save_draft(s,c,run,source,fields,evidence,version);ids.append(d.id)
        result={'status':'DRAFTS_CREATED','draft_ids':ids,'count':len(ids),'requires_manual_submit':True,'content_hash':fingerprint}
        audit(s,c,'correction.return',run.id,result);return result
    route='correction-return:'+run.id
    duplicate=s.scalar(select(Idempotency.id).where(Idempotency.org_id==c.org_id,Idempotency.user_id==c.user_id,Idempotency.route==route,Idempotency.key==fingerprint)) is not None
    return {**idempotent(s,c,route,fingerprint,{'content_hash':fingerprint},apply),'duplicate':duplicate}
