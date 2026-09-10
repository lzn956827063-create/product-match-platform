"""Prepare disjoint unseen-entity retrieval manifests from WDC-style entity-labeled JSONL.

Supply field names explicitly: record ID, entity ID and name. Entity-level labels must
be complete for the chosen subset. Source file and usage terms stay in the manifest.
"""
import argparse
import gzip
import json
import random
from collections import defaultdict
from pathlib import Path
from packages.matching.normalize import digest


def prepare(source,output,id_field,entity_field,name_field,source_url,license_note):
    source=Path(source);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    groups=defaultdict(list);seen=set();seen_names=set()
    opener=gzip.open if source.suffix=='.gz' else open
    with opener(source,'rt',encoding='utf-8') as f:
        for line in f:
            r=json.loads(line);ident=str(r[id_field]);entity=str(r[entity_field]);name=str(r[name_field]).strip()
            if ident in seen:raise ValueError('Duplicate record ID')
            seen.add(ident)
            # Remove exactly identical name duplicates inside each entity before constructing queries.
            if not name or (entity,name.lower()) in seen_names:continue
            seen_names.add((entity,name.lower()))
            groups[entity].append({'id':ident,'entity_id':entity,'fields':{'name':name}})
    entities=sorted(k for k,v in groups.items() if len(v)>=2);random.Random(42).shuffle(entities)
    n=len(entities);train=entities[:int(n*.6)];valid=entities[int(n*.6):int(n*.8)];test=entities[int(n*.8):]
    assert len(test)>=10,'At least 10 multi-record test entities required'
    splits={'train':train,'valid':valid,'test':test}
    (output/'entity-splits.json').write_text(json.dumps(splits,indent=2))
    for ratio in [.1,.3]:
        removed=set(random.Random(42).sample(test,round(len(test)*ratio)))
        catalog=[];queries=[]
        for entity in test:
            records=sorted(groups[entity],key=lambda r:r['id'])
            if entity not in removed:catalog.append(records[0])
            queries.append(records[1])
        manifest={'catalog':catalog,'queries':queries,'provenance':{'source_url':source_url,'source_sha256':digest(source.read_bytes()),'license':license_note,'complete_entity_labels':True,'construction':'one catalog representative and one disjoint query per held-out test entity; exact duplicate names removed','near_duplicate_audit':'not manually audited; report as exploratory','no_match_construction':'controlled removal of declared test entities from catalog','removed_entities':sorted(removed),'no_match_fraction':ratio,'split_manifest_sha256':digest(splits),'seed':42}}
        (output/f'retrieval-no-match-{int(ratio*100)}.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('--output',required=True);p.add_argument('--id-field',required=True);p.add_argument('--entity-field',required=True);p.add_argument('--name-field',required=True);p.add_argument('--source-url',required=True);p.add_argument('--license-note',required=True);a=p.parse_args();prepare(a.source,a.output,a.id_field,a.entity_field,a.name_field,a.source_url,a.license_note)
