"""Inventory stored objects into the quota ledger during maintenance or upgrade.
Counts each physical object once, including legacy exports/cache and orphan bytes.
Does not delete data or reject existing content when already over quota.
Pause writes during a full inventory for a consistent operational report.
"""
import json,os
from sqlalchemy import select
from packages.domain import storage
from packages.domain.config import DATA_DIR,STORAGE_BACKEND
from packages.domain.db import transaction
from packages.domain.models import Organization,QuotaReservation


def main():
    report=[]
    with transaction(write=True) as s:
        for org in s.scalars(select(Organization).with_for_update()):
            objects={}
            if STORAGE_BACKEND=='s3':
                for page in storage.client().get_paginator('list_objects_v2').paginate(Bucket=os.getenv('S3_BUCKET','product-match'),Prefix=org.id+'/'):
                    objects.update({x['Key']:x['Size'] for x in page.get('Contents',[])})
            else:
                root=DATA_DIR/'objects'/org.id
                if root.exists():objects={str(p.relative_to(DATA_DIR/'objects')):p.stat().st_size for p in root.rglob('*') if p.is_file()}
            existing={r.resource_key:r for r in s.scalars(select(QuotaReservation).where(QuotaReservation.org_id==org.id,QuotaReservation.kind=='storage'))}
            for key,amount in objects.items():
                if key in existing:existing[key].amount=amount;existing[key].released=False
                else:s.add(QuotaReservation(org_id=org.id,resource_key=key,kind='storage',amount=amount))
            for key,row in existing.items():
                if key not in objects:row.released=True
            report.append({'org_id':org.id,'objects':len(objects),'bytes':sum(objects.values())})
    return {'scope':'Full physical inventory at maintenance boundary','organizations':report}

if __name__=='__main__':print(json.dumps(main(),indent=2))
