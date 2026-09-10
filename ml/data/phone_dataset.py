"""Controlled simulation, independent generative entity truth; no human labels."""
import argparse
import json
import random
from itertools import product
from pathlib import Path
from packages.matching.normalize import digest


def generate(output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    catalog, queries, records = [], [], []
    # Invented model designations and configurations; these are not manufacturer facts.
    for series in range(20):
        brand = ['A牌','B牌'][series % 2]
        for variant, (storage, region, pack) in enumerate(product(['128','256','512','1024','2048'], ['国行','港版'], ['1','2'])):
            index = series*20 + variant
            entity = f'ENTITY-{index:04d}'
            fields = {'name':f'{brand} X{100+series} 12+{storage} 黑色 {region} {pack}台装','brand':brand,'model':f'x{100+series}','ram':'12','storage':storage,'color':'黑色','region':region,'pack_count':pack,'price':str(2000+index),'currency':'CNY'}
            records.append((entity, f'x{100+series}', fields))
    # Entire final two series are held out. Random entity split within other series.
    pool = [e for e, series, f in records if int(series[1:])<118]
    random.Random(42).shuffle(pool)
    partitions={}
    start=0
    for name,n in [('train',120),('tuning',40),('calibration',40),('threshold',40),('test',120)]:
        partitions[name]=sorted(pool[start:start+n]);start+=n
    partitions['series_holdout']=[e for e,series,f in records if int(series[1:])>=118]
    split_by_entity={e:name for name,entities in partitions.items() for e in entities}
    removed={e for i,(e,_,_) in enumerate(records) if i%10==0}
    for idx,(entity,series,fields) in enumerate(records):
        if entity not in removed:
            catalog.append({'id':'STD-'+entity,'entity_id':entity,'series':series,'fields':fields})
        for variant in range(3):
            qfields=dict(fields);slices=[]
            if variant==1:
                qfields['name']=qfields['name'].replace('黑色','black').replace('台装','件装').replace('X','ｘ').replace(' ','  ')
                slices.append('alias')
            elif variant==2:
                if idx%2:
                    qfields['color']='';qfields['name']=f'{fields["brand"]} {series} {fields["ram"]}+{fields["storage"]} {fields["region"]} {fields["pack_count"]}台装';slices.append('missing')
                else:
                    qfields['storage']='64';slices.append('conflict')
            if entity in removed:slices.append('no_match')
            if split_by_entity[entity] in ('test','series_holdout'):slices.append('unseen_entity')
            queries.append({'id':f'QUERY-{idx:04d}-{variant}','query_id':f'QUERY-{idx:04d}-{variant}','entity_id':entity,'series':series,'catalog_version':'phone-sim-v1','source':'controlled_generator','fields':qfields,'catalog_exists':entity not in removed,'match_evidence':{'method':'independent generative entity assignment','canonical_fields':fields},'annotators':[],'review_status':'SIMULATED','partition':split_by_entity[entity],'slices':slices})
    manifest={'version':'phone-sim-v1','catalog':catalog,'queries':queries,'partitions':partitions,'provenance':{'type':'controlled_simulation','complete_entity_labels':True,'source':'Invented phone SKU combinations, not actual manufacturers or customers','license':'Project-generated controlled data; may be redistributed with this project','obtained_at':'2026-09-10','ground_truth':'Assigned before matching; never derived from matcher predictions','removed_entities':sorted(removed),'no_match_construction':'All catalog records for removed entities are absent; adjacent capacity/region/pack variants remain','annotation':'No human annotators. Not eligible for business-policy admission.','generator_hash':digest(Path(__file__).read_bytes())}}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    (output/'freeze.json').write_text(json.dumps({'version':manifest['version'],'sha256':digest(manifest),'partitions':partitions,'catalog_entities':len(catalog),'query_records':len(queries),'eligible_for_human_test':False},indent=2))
    print({'catalog':len(catalog),'queries':len(queries),'hash':digest(manifest)})
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default='ml/datasets/phone-sim-v1');a=p.parse_args();generate(a.output)
