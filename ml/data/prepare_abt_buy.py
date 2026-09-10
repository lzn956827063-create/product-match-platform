"""Fetch the exact public archive into a local, ignored data directory."""
import argparse
import hashlib
import json
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

URL = "https://pages.cs.wisc.edu/~anhai/data1/deepmatcher_data/Textual/Abt-Buy/abt_buy_exp_data.zip"


def prepare(destination, archive=None):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    archive=Path(archive) if archive else destination/"abt_buy_exp_data.zip"
    if not archive.exists():
        with urllib.request.urlopen(URL,timeout=60) as response:
            archive.write_bytes(response.read(20*1024*1024))
    files={}
    with zipfile.ZipFile(archive) as z:
        for name in ['tableA.csv','tableB.csv','train.csv','valid.csv','test.csv']:
            raw=z.read('exp_data/'+name)
            (destination/name).write_bytes(raw)
            files[name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    manifest={"dataset":"DeepMatcher Abt-Buy textual, official pairwise split","source_url":URL,"documentation":"https://github.com/anhaidgroup/deepmatcher/blob/master/Datasets.md","original_source":"https://dbs.uni-leipzig.de/en/research/projects/object_matching/fever/benchmark_datasets_for_entity_resolution","downloaded_at":datetime.now(timezone.utc).isoformat(),"archive_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),"files":files,"license":"No explicit data license is included in this archive. Record this limitation; raw data is not redistributed with this project. Consult original data providers for reuse terms.","labels":"Official labeled candidate pairs; unlabelled cross-products are not negatives.","scope":"Pair classification only. This split does not support complete retrieval Recall@20 or SKU recommendation coverage claims."}
    (destination/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',default='ml/data/downloads/abt-buy');parser.add_argument('--archive');args=parser.parse_args()
    print(json.dumps(prepare(args.output,args.archive),ensure_ascii=False,indent=2))
