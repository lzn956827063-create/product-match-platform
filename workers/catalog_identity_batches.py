"""Idempotent immutable identity binding in bounded transactions."""
from uuid import uuid5,NAMESPACE_URL
from sqlalchemy import select,update
from packages.domain.db import transaction,uid
from packages.domain.models import *
from packages.domain.errors import require
from packages.matching.normalize import digest


def identity_id(org,version,product):return str(uuid5(NAMESPACE_URL,'product-match:identity:'+org+':'+version+':'+product))


def bind_change(ident,expected):
    with transaction() as s:
        job=s.get(CatalogChange,ident);org=job.org_id
        prior={p.product_id:p.identity_id for p in s.scalars(select(ProductIdentity).where(ProductIdentity.org_id==org,ProductIdentity.version_id==job.from_version_id))}
        known={p.product_id:p.identity_id for p in s.scalars(select(ProductIdentity).where(ProductIdentity.org_id==org,ProductIdentity.version_id==job.to_version_id))}
        inverse={new:old for old,new in job.links.items()};bindings=[]
        for product in s.scalars(select(Product.id).where(Product.org_id==org,Product.version_id==job.from_version_id)):
            if product not in prior:prior[product]=identity_id(org,job.from_version_id,product);bindings.append((product,prior[product],job.from_version_id))
        for product in s.scalars(select(Product.id).where(Product.org_id==org,Product.version_id==job.to_version_id)):
            identity=prior[inverse[product]] if product in inverse else known.get(product) or identity_id(org,job.to_version_id,product)
            if product in known:require(known[product]==identity,409,'IDENTITY_FROZEN','商品身份已绑定，不能改变历史对应')
            else:bindings.append((product,identity,job.to_version_id))
        catalog=job.catalog_id;before_id=job.from_version_id;after_id=job.to_version_id
        version_hashes={x.id:x.content_hash for x in s.scalars(select(CatalogVersion).where(CatalogVersion.id.in_([before_id,after_id])))}
    for start in range(0,len(bindings),500):
        chunk=bindings[start:start+500]
        with transaction(write=True) as s:
            if s.bind.dialect.name=='postgresql':
                from sqlalchemy.dialects.postgresql import insert
            else:from sqlalchemy.dialects.sqlite import insert
            # Catalog version locks serialize competing binders without blocking review rows.
            for version in sorted({x[2] for x in chunk}):s.scalar(select(CatalogVersion).where(CatalogVersion.org_id==org,CatalogVersion.id==version).with_for_update())
            actual_hashes=dict(s.execute(select(CatalogVersion.id,CatalogVersion.content_hash).where(CatalogVersion.org_id==org,CatalogVersion.id.in_([before_id,after_id]))).all())
            require(actual_hashes==version_hashes,409,'CHANGE_INPUT_CHANGED','输入内容已变化')
            identities={x[1] for x in chunk}
            s.execute(insert(CatalogIdentity).on_conflict_do_nothing(index_elements=['id']),[{'id':i,'org_id':org,'catalog_id':catalog} for i in identities])
            values=[{'id':uid(),'org_id':org,'product_id':p,'identity_id':i,'version_id':v} for p,i,v in chunk]
            s.execute(insert(ProductIdentity).on_conflict_do_nothing(index_elements=['org_id','product_id']),values)
            actual=dict(s.execute(select(ProductIdentity.product_id,ProductIdentity.identity_id).where(ProductIdentity.org_id==org,ProductIdentity.product_id.in_([x[0] for x in chunk]))).all())
            require(all(actual.get(p)==i for p,i,_ in chunk),409,'IDENTITY_FROZEN','并发绑定的身份不一致')
            s.execute(update(CatalogChange).where(CatalogChange.id==ident,CatalogChange.org_id==org,CatalogChange.status!='SUCCEEDED').values(report={'phase':'binding','processed':min(start+500,len(bindings)),'total_bindings':len(bindings)}))
        # Let queued short transactions acquire SQLite's single writer between batches.
        import time
        time.sleep(.003)
