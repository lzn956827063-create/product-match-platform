from collections import defaultdict
from sqlalchemy import select, insert
from .models import *
from .auth import scoped
from .db import uid
from ..matching.normalize import digest
from .errors import require
from .services import audit
from .releases import touch_batch

IDENTITY_FIELDS=('brand','model','ram','storage','color','region','pack_count')


def ensure_identities(s,c,version):
    existing={x.product_id for x in s.scalars(select(ProductIdentity).where(ProductIdentity.org_id==c.org_id,ProductIdentity.version_id==version.id))}
    identities=[];links=[]
    for p in s.scalars(select(Product).where(Product.org_id==c.org_id,Product.version_id==version.id)):
        if p.id not in existing:
            identity=uid();identities.append(dict(id=identity,org_id=c.org_id,catalog_id=version.catalog_id));links.append(dict(id=uid(),org_id=c.org_id,product_id=p.id,identity_id=identity,version_id=version.id))
    for start in range(0,len(identities),500):
        s.execute(insert(CatalogIdentity),identities[start:start+500]);s.execute(insert(ProductIdentity),links[start:start+500])
    s.flush()


def bind_identities(s,c,before,after,links):
    ensure_identities(s,c,before)
    prior={x.product_id:x.identity_id for x in s.scalars(select(ProductIdentity).where(ProductIdentity.org_id==c.org_id,ProductIdentity.version_id==before.id))}
    known={x.product_id:x for x in s.scalars(select(ProductIdentity).where(ProductIdentity.org_id==c.org_id,ProductIdentity.version_id==after.id))}
    inverse={new:old for old,new in links.items()};identities=[];bindings=[]
    for p in s.scalars(select(Product).where(Product.org_id==c.org_id,Product.version_id==after.id)):
        if p.id in inverse:identity=prior[inverse[p.id]]
        elif p.id in known:identity=known[p.id].identity_id
        else:
            identity=uid();identities.append(dict(id=identity,org_id=c.org_id,catalog_id=after.catalog_id))
        if p.id in known:require(known[p.id].identity_id==identity,409,'IDENTITY_FROZEN','此商品身份已绑定，不能改变历史对应')
        else:bindings.append(dict(id=uid(),org_id=c.org_id,product_id=p.id,identity_id=identity,version_id=after.id))
    for start in range(0,len(identities),500):s.execute(insert(CatalogIdentity),identities[start:start+500])
    for start in range(0,len(bindings),500):s.execute(insert(ProductIdentity),bindings[start:start+500])
    s.flush()


def create_change(s,c,from_id,to_id,links):
    c.permit('admin')
    before=scoped(s,CatalogVersion,from_id,c);after=scoped(s,CatalogVersion,to_id,c)
    require(before.catalog_id==after.catalog_id and before.id!=after.id and after.number>before.number,422,'CATALOG_PAIR','请选择同一商品库中按版本顺序排列的两个版本')
    predecessor=s.scalar(select(CatalogChange).where(CatalogChange.org_id==c.org_id,CatalogChange.to_version_id==from_id))
    require(not predecessor or predecessor.status=='SUCCEEDED',409,'PREDECESSOR_PENDING','请先完成前驱版本的身份分析')
    old=s.scalar(select(CatalogChange).where(CatalogChange.org_id==c.org_id,CatalogChange.from_version_id==from_id,CatalogChange.to_version_id==to_id))
    if old:
        require(old.links==links,409,'CHANGE_IMMUTABLE','此版本对已建立分析任务，身份对应不可替换')
        return old
    require(not s.scalar(select(CatalogChange.id).where(CatalogChange.org_id==c.org_id,CatalogChange.to_version_id==to_id)),409,'VERSION_ALREADY_LINKED','目标版本已有明确的前驱版本，不能重新定义商品身份')
    products_before=set(s.scalars(select(Product.id).where(Product.org_id==c.org_id,Product.version_id==from_id)))
    products_after=set(s.scalars(select(Product.id).where(Product.org_id==c.org_id,Product.version_id==to_id)))
    require(set(links)<=products_before and set(links.values())<=products_after and len(set(links.values()))==len(links),422,'INVALID_IDENTITY_LINKS','商品身份对应必须是一对一且属于所选版本')
    # Persist the request; identity writes and impact analysis run asynchronously.
    obj=CatalogChange(id=uid(),org_id=c.org_id,catalog_id=before.catalog_id,from_version_id=from_id,to_version_id=to_id,links=links,input_hash=digest([before.content_hash,after.content_hash,links]),created_by=c.user_id)
    s.add(obj);s.flush();s.add(Outbox(org_id=c.org_id,event_key='catalog_change:'+obj.id,kind='catalog_change',resource_id=obj.id));audit(s,c,'catalog.change.request',obj.id,{'explicit_identity_links':len(links)})
    return obj


def release_problem_map(s,c,products):
    products=list(products)
    if not products:return {}
    activations=list(s.scalars(select(CatalogActivation).where(CatalogActivation.org_id==c.org_id)))
    if not activations:return {}
    versions={v.id:v.catalog_id for v in s.scalars(select(CatalogVersion).where(CatalogVersion.org_id==c.org_id))}
    active={x.catalog_id:x.version_id for x in activations}
    identities={p.product_id:p.identity_id for p in s.scalars(select(ProductIdentity).where(ProductIdentity.org_id==c.org_id,ProductIdentity.product_id.in_([p.id for p in products])))}
    current={x.identity_id:p for x,p in s.execute(select(ProductIdentity,Product).join(Product,(Product.id==ProductIdentity.product_id)&(Product.org_id==ProductIdentity.org_id)).where(ProductIdentity.org_id==c.org_id,ProductIdentity.version_id.in_(active.values())))}
    result={}
    for p in products:
        active_id=active.get(versions[p.version_id])
        if not active_id or active_id==p.version_id:continue
        new=current.get(identities.get(p.id))
        if not new:result[p.id]={'code':'PRODUCT_RETIRED','message':'目标商品在当前启用的标准库中已停用或身份尚未对应'}
        elif p.sku!=new.sku or any(p.normalized.get(f)!=new.normalized.get(f) for f in IDENTITY_FIELDS):result[p.id]={'code':'CATALOG_REVIEW_REQUIRED','message':'商品编码或身份字段已变化，需要按新标准库重新匹配审核'}
    return result


def product_release_problem(s,c,product):
    return release_problem_map(s,c,[product]).get(product.id)


def activate(s,c,change):
    c.permit('admin')
    require(change.status=='SUCCEEDED',409,'CHANGE_NOT_READY','请先完成标准库影响分析')
    version=scoped(s,CatalogVersion,change.to_version_id,c)
    require(version.status=='PUBLISHED',409,'CATALOG_NOT_PUBLISHED','目标标准库尚未发布')
    current=s.scalar(select(CatalogActivation).where(CatalogActivation.org_id==c.org_id,CatalogActivation.catalog_id==change.catalog_id))
    require(not current or current.version_id in (change.from_version_id,change.to_version_id),409,'ACTIVATION_CHAIN','当前启用版本已变化，请从当前版本建立影响分析')
    if current and current.version_id==version.id:return
    if current:current.version_id=version.id
    else:s.add(CatalogActivation(org_id=c.org_id,catalog_id=change.catalog_id,version_id=version.id))
    for batch in s.scalars(select(Batch).where(Batch.org_id==c.org_id)):touch_batch(s,c.org_id,batch.id)
    audit(s,c,'catalog.activate',change.id,{'version_id':version.id})
