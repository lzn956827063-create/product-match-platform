"""Local procurement protocol simulator, never a claim of real ERP integration.

Configuration is a private JSON file: api_url, org_id, credential, signing_secret,
plus optional fail_first_notifications and partial_keys. Do not commit credentials.
"""
import argparse,csv,hashlib,io,json,sqlite3,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import httpx
from packages.domain.integrations import verify_signature
from scripts import delta_receiver


def main(config_path,state_path,port):
    config_path=Path(config_path);state_path=Path(state_path);state_path.parent.mkdir(parents=True,exist_ok=True)
    lock=threading.Lock()
    def db():
        s=sqlite3.connect(state_path);s.execute('PRAGMA journal_mode=WAL');return s
    with db() as s:
        s.executescript('CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, body_hash TEXT, delivery_id TEXT, receipt TEXT, acknowledged INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY, release_id TEXT, number INTEGER, invalidated INTEGER); CREATE TABLE IF NOT EXISTS mappings(batch_id TEXT, stable_key TEXT, row_data TEXT, PRIMARY KEY(batch_id,stable_key)); CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, at REAL, status INTEGER);')
        delta_receiver.initialize(s)
    def config():return json.loads(config_path.read_text())
    def client(c):return httpx.Client(base_url=c['api_url'].rstrip('/'),headers={'Authorization':'Bearer '+c['credential'],'X-Organization-ID':c['org_id']},timeout=15)
    def return_receipts():
        while True:
            try:
                c=config()
                if c.get('hold_receipts'):time.sleep(2);continue
                with db() as s:pending=s.execute('SELECT event_id,delivery_id,receipt FROM events WHERE acknowledged=0').fetchall()
                with client(c) as api:
                    for event,delivery,receipt in pending:
                        r=api.post('/service/deliveries/'+delivery+'/receipt',json=json.loads(receipt))
                        if r.status_code==200:
                            with db() as s:s.execute('UPDATE events SET acknowledged=1 WHERE event_id=?',(event,))
            except Exception:pass
            time.sleep(2)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path not in ('/notify','/receipt-query'):self.send_error(404);return
            length=int(self.headers.get('Content-Length',0))
            if not 0<length<=65536:self.send_error(413);return
            body=self.rfile.read(length);event_id=self.headers.get('X-Event-ID','');c=config()
            if not verify_signature(c['signing_secret'],event_id,self.headers.get('X-Timestamp',''),body,self.headers.get('X-Signature','')):self.send_error(401);return
            try:payload=json.loads(body)
            except ValueError:self.send_error(400);return
            if self.path=='/receipt-query':
                with db() as s:row=s.execute('SELECT receipt FROM events WHERE event_id=?',(event_id,)).fetchone()
                if not row:self.send_error(404);return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(row[0].encode());return
            if payload.get('org_id')!=c['org_id'] or payload.get('event_id')!=event_id:self.send_error(403);return
            hashed=hashlib.sha256(body).hexdigest()
            with lock:
                with db() as s:
                    old=s.execute('SELECT body_hash,receipt FROM events WHERE event_id=?',(event_id,)).fetchone();attempts=s.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]
                    if old and old[0]!=hashed:self.send_error(409);return
                    if attempts<c.get('fail_first_notifications',0):s.execute('INSERT INTO attempts(event_id,at,status) VALUES(?,?,503)',(event_id,time.time()));self.send_error(503);return
                    # Already-applied events return the same receipt without reapplying rows.
                    if old and json.loads(old[1])['status']=='APPLIED':s.execute('UPDATE events SET acknowledged=0 WHERE event_id=?',(event_id,));s.execute('INSERT INTO attempts(event_id,at,status) VALUES(?,?,202)',(event_id,time.time()));self.send_response(202);self.end_headers();return
                try:
                    with client(c) as api:
                        response=api.get('/service/releases/'+payload['release_id']);response.raise_for_status();release=response.json()
                        use_delta=False
                        if payload['kind']=='ARTIFACTS_READY':
                            with db() as s:baseline=s.execute('SELECT release_id,number,invalidated FROM batches WHERE batch_id=?',(release['batch_id'],)).fetchone()
                            if c.get('use_delta',True) and baseline and not baseline[2] and baseline[0]==release['base_release_id'] and baseline[1]<release['number']:
                                offset=0
                                while offset is not None:
                                    response=api.get('/service/releases/'+release['id']+'/delta',params={'base_release_id':baseline[0],'offset':offset,'limit':c.get('delta_page_size',100)})
                                    if response.status_code==409:break
                                    response.raise_for_status();page=response.json()
                                    with db() as s:delta_receiver.stage(s,event_id,page,offset)
                                    if c.get('interrupt_after_page')==offset:raise ValueError('CONTROLLED_PAGE_INTERRUPTION')
                                    offset=page['next_offset']
                                use_delta=offset is None
                            if not use_delta:
                                download=api.get('/service/releases/'+release['id']+'/artifacts/full/download');download.raise_for_status()
                                if hashlib.sha256(download.content).hexdigest()!=download.headers['X-File-SHA256']:raise ValueError('HASH_MISMATCH')
                                rows=list(csv.DictReader(io.StringIO(download.content.decode('utf-8-sig'))))
                            else:rows=[]
                        else:rows=[]
                    with db() as s:
                        previous=s.execute('SELECT release_id,number FROM batches WHERE batch_id=?',(release['batch_id'],)).fetchone();failed=[]
                        if payload['kind'] in ('REVOKED','INVALIDATED'):
                            if previous and previous[0]==release['id']:s.execute('UPDATE batches SET invalidated=1 WHERE batch_id=?',(release['batch_id'],))
                        elif use_delta and (not previous or release['number']>previous[1]):
                            if c.get('partial_keys'):raise ValueError('CONTROLLED_ATOMIC_RETRY')
                            delta_receiver.apply(s,event_id,release['batch_id'],release['id'],release['number'])
                            s.execute('UPDATE batches SET release_id=?,number=?,invalidated=? WHERE batch_id=?',(release['id'],release['number'],int(release['invalidated']),release['batch_id']))
                        elif not previous or release['number']>=previous[1]:
                            failed=[{'stable_key':r['stable_key'],'reason':'受控模拟单条失败'} for r in rows if r['stable_key'] in c.get('partial_keys',[])]
                            s.execute('DELETE FROM mappings WHERE batch_id=?',(release['batch_id'],))
                            s.executemany('INSERT INTO mappings(batch_id,stable_key,row_data) VALUES(?,?,?)',[(release['batch_id'],r['stable_key'],json.dumps(r,ensure_ascii=False)) for r in rows if r['stable_key'] not in c.get('partial_keys',[])])
                            s.execute('INSERT INTO batches VALUES(?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET release_id=excluded.release_id,number=excluded.number,invalidated=excluded.invalidated',(release['batch_id'],release['id'],release['number'],int(release['invalidated'] or release['status']=='REVOKED')))
                        older=previous and previous[1]>release['number'] and payload['kind']=='ARTIFACTS_READY'
                        receipt={'event_id':event_id,'status':'REJECTED' if older else 'PARTIAL' if failed else 'APPLIED','failed_rows':failed,'message':'模拟采购接收端已处理；非真实 ERP','applied_release_id':release['id'],'base_release_id':release['base_release_id'],'snapshot_hash':release['snapshot_hash']}
                        s.execute('INSERT INTO events(event_id,body_hash,delivery_id,receipt) VALUES(?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET receipt=excluded.receipt,acknowledged=0',(event_id,hashed,payload['delivery_id'],json.dumps(receipt)))
                        s.execute('INSERT INTO attempts(event_id,at,status) VALUES(?,?,202)',(event_id,time.time()))
                except Exception:self.send_error(503);return
            self.send_response(202);self.end_headers()
        def log_message(self,*args):pass
    threading.Thread(target=return_receipts,daemon=True).start()
    print(json.dumps({'listening':f'http://127.0.0.1:{port}/notify','scope':'local procurement protocol simulation','state':str(state_path)}),flush=True)
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--state',required=True);p.add_argument('--port',type=int,default=18769);a=p.parse_args();main(a.config,a.state,a.port)
