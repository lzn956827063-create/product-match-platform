"""Explicit, idempotent demo installation. Never invoked by API startup."""
import csv
import io
import os
from pathlib import Path
from sqlalchemy import select

from packages.domain import storage
from packages.domain.auth import Context, password_hash
from packages.domain.db import initialize, transaction, uid
from packages.domain.ingest import FIELDS, parse_file
from packages.domain.models import *
from packages.domain.services import create_catalog_version, create_revision, create_run
from packages.matching.engine import DEFAULT_POLICY
from packages.matching.normalize import digest
from workers.jobs import process_run

HEADERS = ["商品编号", "商品名称", "品牌", "型号", "运行内存", "存储容量", "颜色", "销售版本", "包装数量", "报价", "币种"]
MAPPING = dict(zip(["sku", "name", "brand", "model", "ram", "storage", "color", "region", "pack_count", "price", "currency"], HEADERS))


def sample_rows():
    specs = [("小米", "xiaomi 15", "12", "256", "黑色", "国行"), ("小米", "xiaomi 15", "16", "512", "白色", "国行"), ("三星", "galaxy s25", "12", "256", "银色", "国行"), ("三星", "galaxy s25 ultra", "12", "512", "黑色", "国行"), ("荣耀", "honor 400", "12", "256", "蓝色", "国行"), ("华为", "pura 80", "12", "256", "黑色", "国行"), ("OPPO", "find x8", "12", "256", "白色", "国行"), ("vivo", "x200", "12", "256", "蓝色", "国行"), ("A牌", "x5", "8", "128", "黑色", "国行"), ("A牌", "x5", "12", "256", "黑色", "国行"), ("A牌", "x6-pro", "12", "256", "黑色", "国行"), ("红米", "note 14", "8", "128", "黑色", "国行")]
    products = []
    for i, (brand, model, ram, cap, color, region) in enumerate(specs, 1):
        products.append([f"STD-{i:04d}", f"{brand} {model.upper()} {ram}+{cap} {color} {region} 单机", brand, model, ram, cap, color, region, "1", str(1599+i*200), "CNY"])
    sources = []
    for i in range(28):
        p = products[i % len(products)].copy()
        p[0] = f"{i+1:06d}"
        p[9] = str(int(p[9])-60)
        if i < 12:
            p[1] = p[1].replace("单机", "原封 单台")
        elif i < 17:
            p[5] = "1024"
            p[1] = f"{p[2]} {p[3]} {p[4]}+1024 {p[6]} {p[7]} 单机"
        elif i < 22:
            p[6] = ""
            p[1] = f"{p[2]} {p[3]} {p[4]}+{p[5]} {p[7]} 单机"
        elif i < 25:
            p[7] = "港版"
            p[1] = p[1].replace("国行", "港版")
        else:
            p[8] = "2"
            p[1] = p[1].replace("单机", "2台装")
        sources.append(p)
    sources.append(["000029", "ZZZ Q777 限量设备", "ZZZ", "q777", "64", "4096", "橙色", "国际版", "1", "999", "CNY"])
    sources.append(["000030", "", "", "", "", "", "", "", "", "", ""])
    return products, sources


def csv_bytes(rows):
    buff = io.StringIO()
    w = csv.writer(buff)
    w.writerow(HEADERS)
    w.writerows(rows)
    return buff.getvalue().encode("utf-8-sig")


def add_file(s, org_id, name, content):
    f = File(id=uid(), org_id=org_id, name=name, object_key=storage.quota_put(org_id, content, "csv", s=s), sha256=digest(content), size=len(content), encoding="utf-8", sheets=["CSV"])
    s.add(f)
    s.flush()
    return f


def main():
    initialize()
    password = os.getenv("DEMO_PASSWORD", "Demo2026!match")
    product_rows, source_rows = sample_rows()
    samples = Path(__file__).resolve().parents[1] / "samples"
    if os.access(samples if samples.exists() else samples.parent, os.W_OK):
        samples.mkdir(exist_ok=True)
        (samples / "标准商品库.csv").write_bytes(csv_bytes(product_rows))
        (samples / "供应商商品表.csv").write_bytes(csv_bytes(source_rows))
    run_ids = []
    with transaction(write=True) as s:
        if s.scalar(select(User).where(User.email == "operator@demo.local")):
            print("演示数据已存在；没有修改任何业务数据。")
            return
        for idx, org_name in enumerate(["星云采购 · 演示组织", "远山商贸 · 隔离验证"]):
            org = Organization(id=uid(), name=org_name)
            s.add(org)
            s.flush()
            actors = {}
            for role, name in [("operator", "林晓 · 数据专员"), ("reviewer", "陈然 · 审核员"), ("admin", "周宁 · 管理员")]:
                email = f"{role}@{'demo' if idx == 0 else 'other'}.local"
                u = User(id=uid(), email=email, name=name, password_hash=password_hash(password))
                s.add(u)
                s.flush()
                s.add(Membership(org_id=org.id, user_id=u.id, roles=[role]))
                actors[role] = u
            policy = Policy(id=uid(), org_id=org.id, name="手机 SKU 规则基线 v1", config=DEFAULT_POLICY)
            s.add(policy)
            org.default_policy_id = policy.id
            s.flush()
            admin = Context(org.id, actors["admin"].id, ["admin"])
            catalog = Catalog(id=uid(), org_id=org.id, name="手机标准商品库")
            s.add(catalog)
            s.flush()
            file = add_file(s, org.id, "标准商品库.csv", csv_bytes(product_rows))
            version = create_catalog_version(s, admin, catalog, {"file_id": file.id, "sheet": "CSV", "header_row": 1, "mapping": MAPPING, "exclude_rows": []})
            version.status, version.lock_version = "PUBLISHED", 1
            file = add_file(s, org.id, "供应商商品表.csv", csv_bytes(source_rows))
            operator = Context(org.id, actors["operator"].id, ["operator"])
            batch = Batch(id=uid(), org_id=org.id, name="九月手机采购 · 首批商品核对", supplier="华东数码供应商", created_by=operator.user_id)
            s.add(batch)
            s.flush()
            revision = create_revision(s, operator, batch, {"file_id": file.id, "sheet": "CSV", "header_row": 1, "mapping": MAPPING, "exclude_rows": [31]})
            run = create_run(s, operator, {"revision_id": revision.id, "catalog_version_id": version.id, "policy_id": policy.id})
            run_ids.append(run["id"])
    for ident in run_ids:
        process_run(ident)
    print("已创建两个隔离组织，每个组织含数据专员、审核员和管理员。演示数据为人工构造样例。")


if __name__ == "__main__":
    main()
