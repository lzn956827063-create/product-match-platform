import io
import pytest
from openpyxl import Workbook
from packages.domain.errors import Problem
from packages.domain.ingest import mapped_rows, parse_file
from packages.matching.engine import Matcher
from packages.matching.normalize import capacity, compare, normalize


BASE = dict(name="A牌 X5 8+128 黑色 国行 单机", brand="A牌", model="x5", ram="8", storage="128", color="黑色", region="国行", pack_count="1")


@pytest.mark.parametrize("field,value", [("ram","12"),("storage","256"),("color","白色"),("region","港版"),("pack_count","2"),("model","x6"),("brand","B牌")])
def test_hard_conflicts(field, value):
    source = normalize(BASE)
    target = normalize({**BASE,field:value,"name":"目标商品"})
    assert field in compare(source,target)[1]
    suggestion, candidates = Matcher([{"id":"target", "normalized":target}]).match(source)
    # Explicit model lookup or lexical retrieval may have no candidate, never recommend conflict.
    assert suggestion != "RECOMMENDED"


def test_shorthand_missing_and_units():
    n=normalize({"name":"Ａ牌 X5 黑色 8+128 国行 单机"})
    assert n["ram"]=="8GB" and n["storage"]=="128GB"
    assert normalize({"name":"A牌 X5 黑色"})["ram"] is None
    assert normalize({**BASE,"model":"X5-Pro-5G"})["model"]=="x5-pro-5g"
    assert capacity("1TB")=="1000GB" and capacity("1TiB")=="1024GiB"
    assert capacity("128GB") != capacity("128GiB")


@pytest.mark.parametrize('field,value',[('model','x6'),('color','白色'),('region','港版'),('pack_count','2')])
def test_name_and_structured_identity_conflict(field,value):
    inconsistent=normalize({**BASE,field:value})
    assert any('字段与名称冲突' in issue for issue in inconsistent['issues'])
    assert 'data_quality' in compare(inconsistent,normalize(BASE))[1]


def test_missing_never_recommended_and_price_irrelevant():
    target=normalize(BASE)
    m=Matcher([{"id":"p", "normalized":target}])
    assert m.match(normalize({**BASE,"price":"999999"}))[0]=="RECOMMENDED"
    assert m.match(normalize({**BASE,"price":"价格待询"}))[0]=="RECOMMENDED"
    assert m.match(normalize({**BASE,"ram":"", "name":"A牌 X5 黑色 国行 单机"}))[0]=="REVIEW"
    assert m.match(normalize({"name":"zzzzzz"}))[0]=="NO_CANDIDATE"


def test_no_brand_bucket_and_top20_bound():
    target=normalize(BASE)
    m=Matcher([{"id":str(i),"normalized":target} for i in range(30)])
    state, rows=m.match(normalize({**BASE,"brand":"","name":"X5 8+128 黑色 国行 单机"}))
    assert len(rows)==20 and state=="REVIEW"


def test_csv_quality_and_leading_zero():
    parsed=parse_file('编号,商品名称\n00012,A牌 X5\n00012,\n'.encode(),"a.csv")
    rows=mapped_rows(parsed,"CSV",1,{"sku":"编号","name":"商品名称"})
    assert rows[0]["sku"]=="00012"
    assert any(x["level"]=="error" for x in rows[1]["issues"])
    assert any("重复" in x["message"] for x in rows[1]["issues"])
    parsed=parse_file('名称\n中文商品\n'.encode('gb18030'),"a.csv","gb18030")
    assert parsed['CSV']['rows'][1][0]=='中文商品'


def test_xlsx_sheet_header_formulas_and_number_format():
    wb=Workbook();ws=wb.active;ws.title='商品';ws.append(['说明']);ws.append(['编号','名称']);ws.append([12,'普通商品']);ws['A3'].number_format='000000';ws.append(['000013','=1+1'])
    buf=io.BytesIO();wb.save(buf)
    parsed=parse_file(buf.getvalue(),'a.xlsx')
    rows=mapped_rows(parsed,'商品',2,{'sku':'编号','name':'名称'})
    assert rows[0]['sku']=='000012'
    assert any('公式' in i['message'] for i in rows[1]['issues'])


@pytest.mark.parametrize('data,name',[ (b'PKbad','a.csv'),(b'notzip','a.xlsx'),(b'hello','a.exe'),(b'a'* (20*1024*1024+1),'a.csv') ], ids=['csv_signature','xlsx_signature','unsupported_type','size_limit'])
def test_bad_files_rejected(data,name):
    with pytest.raises(Problem):parse_file(data,name)
