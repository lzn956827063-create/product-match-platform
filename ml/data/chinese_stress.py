"""200 constructed regression pairs. This is not a human-verified test dataset."""
import json
from pathlib import Path
from packages.matching.normalize import normalize,compare,digest


def build():
    pairs=[]
    for i in range(20):
        base={'name':f'A牌 X{i+10} 8+128 黑色 国行 单机','brand':'A牌','model':f'x{i+10}','ram':'8','storage':'128','color':'黑色','region':'国行','pack_count':'1'}
        variants=[('same',dict(base)),('alias',{**base,'name':f'Ａ牌 X{i+10} black 8+128 国行 单机','color':'black'}),('capacity',{**base,'name':'其他容量版本','storage':'256'}),('ram',{**base,'name':'其他内存版本','ram':'12'}),('color',{**base,'name':'其他颜色版本','color':'白色'}),('region',{**base,'name':'港版机型','region':'港版'}),('bundle',{**base,'name':'双机套装','pack_count':'2'}),('model_suffix',{**base,'name':'型号后缀不同','model':base['model']+'-pro'}),('gib',{**base,'name':'二进制容量','storage':'128GiB'}),('missing',{**base,'name':'资料不完整','ram':''})]
        for kind,right in variants:
            pairs.append({'id':f'{i:02d}-{kind}','left':base,'right':right,'expected':'same' if kind in ('same','alias') else 'missing' if kind=='missing' else 'conflict'})
    errors=[]
    for p in pairs:
        _,conflicts,missing=compare(normalize(p['left']),normalize(p['right']))
        actual='conflict' if conflicts else 'missing' if missing else 'same'
        if actual!=p['expected']:errors.append({'id':p['id'],'actual':actual,'expected':p['expected']})
    return {'provenance':'Generated Chinese SKU regression fixtures with declared expected rules; no human labelling or real-world accuracy claim.','pairs':pairs}, {'n':len(pairs),'passed':len(pairs)-len(errors),'failures':errors,'dataset_hash':digest(pairs),'status':'constructed regression only'}


if __name__=='__main__':
    data,result=build();Path('samples/中文规格回归样例.json').write_text(json.dumps(data,ensure_ascii=False,indent=2));Path('docs/chinese-regression.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
