"""Remove the restore delivery hold only after external reconciliation is acknowledged."""
import argparse,json
from packages.domain.config import DATA_DIR
from packages.domain.db import now


def main():
    p=argparse.ArgumentParser();p.add_argument('--acknowledge-reconciliation',action='store_true',required=True);p.add_argument('--reason',required=True);a=p.parse_args()
    if len(a.reason.strip())<5:raise SystemExit('Document the reconciliation evidence and responsible operator')
    flag=DATA_DIR/'delivery-restore-hold.json'
    if not flag.exists():raise SystemExit('No restore hold exists')
    record={**json.loads(flag.read_text()),'released_at':now(),'reason':a.reason}
    (DATA_DIR/'delivery-restore-reconciliation.json').write_text(json.dumps(record,ensure_ascii=False,indent=2));flag.unlink();print('Delivery restore hold released; original event IDs will be retained for any retry.')


if __name__=='__main__':main()
