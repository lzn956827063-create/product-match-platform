"""Build disposable browser acceptance data using the regression scenarios."""
import json,shutil
from pathlib import Path
from tests.conftest import env,TEST_DIR
from tests.test_v13 import impact_fixture,publish_new
from workers.quality import process_quality
from packages.domain.db import transaction,uid
from packages.domain.models import *
from sqlalchemy import select
from packages.domain import integrations
import time

def main(destination):
    generator=env.__wrapped__();value=next(generator)
    try:
        client,h,run,new,old,task,_=impact_fixture(value);replacement=publish_new(client,h,new,old);org=h['admin']['X-Organization-ID']
        with transaction(write=True) as s:
            actor=s.scalar(select(User).where(User.email=='admin@demo.local'));account=ServiceAccount(id=uid(),org_id=org,name='隔离浏览器接收账号',scopes=list(integrations.SCOPES),expires_at=time.time()+86400);integrations.issue_credential(account);s.add(account);s.flush()
            i=Integration(id=uid(),org_id=org,account_id=account.id,name='隔离模拟下游',url='http://127.0.0.1:18890/notify',secret_cipher=integrations.cipher().encrypt(b'isolated-test-key').decode(),active=False,reconciliation_owner_id=actor.id);s.add(i);s.flush()
            event=s.scalar(select(ReleaseEvent).where(ReleaseEvent.release_id==replacement['id'],ReleaseEvent.kind=='ARTIFACTS_READY'));d=Delivery(id=uid(),org_id=org,integration_id=i.id,release_id=replacement['id'],event_id=event.id,kind='ARTIFACTS_READY',status='RECONCILE',received_at=time.time()-1800,receipt_due_at=time.time()-900,escalation_state='ACTION_REQUIRED');s.add(d)
            item=s.scalar(select(Item).where(Item.run_id==run['id']));item.status='NEEDS_INFO'
            supplier=Supplier(id=uid(),org_id=org,name='v1.3 隔离质量测试供应商');s.add(supplier);s.flush();s.add(BatchSupplier(org_id=org,batch_id=run['batch_id'],supplier_id=supplier.id,actor_id=actor.id,reason='隔离测试来源'))
            fixture={'run_id':run['id'],'new_run_id':new['id'],'impact_id':task['id'],'replacement_release_id':replacement['id'],'old_release_id':old['id'],'from_version_id':run['catalog_version_id'],'to_version_id':new['catalog_version_id'],'delivery_id':d.id}
        process_quality(run['revision_id']);folder=Path(destination).resolve();folder.mkdir(parents=True,exist_ok=True)
        import sqlite3
        with sqlite3.connect(TEST_DIR/'tests.db') as src,sqlite3.connect(folder/'app.db') as dst:src.backup(dst)
        for path in TEST_DIR.iterdir():
            if path.is_dir():shutil.copytree(path,folder/path.name,dirs_exist_ok=True)
            elif path.suffix not in ('.db','-wal','-shm') and not path.name.startswith('tests.db'):shutil.copy2(path,folder/path.name)
        (folder/'browser-fixture.json').write_text(json.dumps(fixture));print('Prepared isolated v1.3 browser data')
    finally:generator.close()

if __name__=='__main__':
    import sys
    main(sys.argv[1])
