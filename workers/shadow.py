import time
from sqlalchemy import select
from packages.domain.db import transaction,uid
from packages.domain.models import *
from packages.matching.engine import Matcher, DEFAULT_POLICY
from packages.matching.policy import validate_bundle


def process_shadow(ident):
    with transaction(write=True) as s:
        row=s.scalar(select(ShadowRun).where(ShadowRun.id==ident).with_for_update())
        if not row or row.status in ('SUCCEEDED','FAILED'):return
        if row.status=='RUNNING' and row.report.get('_lease_until',0)>time.time():return
        token=uid();row.status='RUNNING';row.report={'_token':token,'_lease_until':time.time()+120};org=row.org_id
        run=s.scalar(select(Run).where(Run.org_id==org,Run.id==row.run_id))
        evaluation=s.scalar(select(PolicyEvaluation).where(PolicyEvaluation.org_id==org,PolicyEvaluation.id==row.evaluation_id))
        config=evaluation.config
        products=[{'id':p.id,'normalized':p.normalized} for p in s.scalars(select(Product).where(Product.org_id==org,Product.version_id==run.catalog_version_id).order_by(Product.id))]
        sources=[(i.id,src.normalized) for i,src in s.execute(select(Item,Source).join(Source,(Source.org_id==Item.org_id)&(Source.id==Item.source_id)).where(Item.org_id==org,Item.run_id==run.id))]
        decisions={i.id:ev.product_id for i,ev in s.execute(select(Item,ReviewEvent).join(ReviewEvent,(ReviewEvent.org_id==Item.org_id)&(ReviewEvent.id==Item.current_decision_id)).where(Item.org_id==org,Item.run_id==run.id,ReviewEvent.action=='confirm'))}
    try:
        started=time.perf_counter();validate_bundle(config)
        rules=Matcher(products,DEFAULT_POLICY);learning=Matcher(products,config);changes=[];counts={'rules_recommended':0,'learning_recommended':0,'top1_changed':0,'learning_conflicts':0,'human_decisions':len(decisions),'learning_disagrees_with_human':0}
        for index,(item_id,source) in enumerate(sources):
            if index%100==0:
                with transaction(write=True) as s:
                    current=s.scalar(select(ShadowRun).where(ShadowRun.id==ident,ShadowRun.org_id==org).with_for_update())
                    if current.report.get('_token')!=token or current.report.get('_lease_until',0)<=time.time():return
                    current.report={**current.report,'_lease_until':time.time()+120,'processed':index,'total':len(sources)}
            a,aa=rules.match(source);b,bb=learning.match(source)
            atop=aa[0]['product_id'] if aa else None;btop=bb[0]['product_id'] if bb else None
            counts['rules_recommended']+=a=='RECOMMENDED';counts['learning_recommended']+=b=='RECOMMENDED';counts['top1_changed']+=atop!=btop;counts['learning_conflicts']+=b=='CONFLICT'
            counts['learning_disagrees_with_human']+=item_id in decisions and btop!=decisions[item_id]
            if atop!=btop or a!=b:changes.append({'item_id':item_id,'rules_state':a,'learning_state':b,'rules_top1':atop,'learning_top1':btop})
        report={**counts,'n':len(sources),'changes':changes,'seconds':time.perf_counter()-started,'bundle_hash':config['bundle_hash'],'scope':'read-only shadow; existing decisions and exports are unaffected'}
        with transaction(write=True) as s:
            row=s.scalar(select(ShadowRun).where(ShadowRun.id==ident,ShadowRun.org_id==org).with_for_update());
            if row.report.get('_token')!=token or row.report.get('_lease_until',0)<=time.time():return
            row.status,row.report='SUCCEEDED',report
            s.scalar(select(Outbox).where(Outbox.org_id==org,Outbox.event_key=='shadow:'+ident)).completed=True
    except Exception as exc:
        with transaction(write=True) as s:
            row=s.get(ShadowRun,ident)
            if row.report.get('_token')!=token:return
            row.status,row.error='FAILED',type(exc).__name__
            s.scalar(select(Outbox).where(Outbox.org_id==org,Outbox.event_key=='shadow:'+ident)).completed=True
