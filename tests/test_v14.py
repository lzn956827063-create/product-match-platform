import hashlib
import hmac
import os
import time

from sqlalchemy import select

from packages.domain.db import transaction, uid
from packages.domain.models import DatasetVersion, File, IngestionEvent, Membership, RateBucket
from packages.domain.v14_ingestion import directory_path
from packages.matching.normalize import digest
from scripts.poll_ingestion import poll_once


P = "/api/v1"
CSV = "商品编号,商品名称,品牌,型号,运行内存,存储容量,颜色,销售版本,包装数量\nA-1,小米 15 12+256 黑色 国行 单机,小米,15,12,256,黑色,国行,1\n".encode()
BAD_CSV = "商品编号,商品名称\nA-1,\n".encode()
MAPPING = {"sku": "商品编号", "name": "商品名称", "brand": "品牌", "model": "型号", "ram": "运行内存", "storage": "存储容量", "color": "颜色", "region": "销售版本", "pack_count": "包装数量"}


def create_supplier(client, headers):
    response = client.post(P + "/suppliers", headers=headers["admin"], json={"name": "自动接入供应商", "aliases": []})
    assert response.status_code == 201, response.text
    return response.json()


def create_profile(client, headers, supplier, mapping=MAPPING):
    response = client.post(P + "/import-profiles", headers=headers["operator"], json={
        "supplier_id": supplier["id"], "name": "手机清单", "file_type": "csv", "sheet": "CSV", "header_row": 1,
        "encoding": "utf-8", "mapping": mapping, "transformations": {"brand": {"trim": True}},
        "validations": {"required": ["name", "model"], "unique": ["sku"]}, "change_note": "建立供应商首版映射",
    })
    assert response.status_code == 201, response.text
    profile = response.json()
    response = client.post(P + f"/import-profiles/{profile['id']}/publish", headers=headers["admin"], json={"expected_version": 0})
    assert response.status_code == 200, response.text
    return response.json()


def upload(client, headers, content=CSV, name="goods.csv"):
    response = client.post(P + "/files", headers=headers["operator"], files={"file": (name, content, "text/csv")})
    assert response.status_code == 202, response.text
    return response.json()


def test_profile_dry_run_publish_rollback_and_history(env):
    client, headers, _, _ = env
    supplier = create_supplier(client, headers)
    file = upload(client, headers)
    response = client.post(P + "/import-profiles", headers=headers["operator"], json={
        "supplier_id": supplier["id"], "name": "手机清单", "file_type": "csv", "sheet": "CSV", "header_row": 1,
        "encoding": "utf-8", "mapping": MAPPING, "transformations": {}, "validations": {"required": ["name"]}, "change_note": "首版",
    })
    profile = response.json()
    dry = client.post(P + f"/import-profiles/{profile['id']}/dry-run", headers=headers["operator"], json={"file_id": file["id"]})
    assert dry.status_code == 200 and dry.json()["accepted"] == 1 and dry.json()["rejected"] == 0
    published = client.post(P + f"/import-profiles/{profile['id']}/publish", headers=headers["admin"], json={"expected_version": 0})
    assert published.status_code == 200 and published.json()["status"] == "PUBLISHED"
    rolled = client.post(P + f"/import-profiles/{profile['id']}/rollback", headers=headers["admin"], json={"reason": "恢复已验证映射"})
    assert rolled.status_code == 201 and rolled.json()["number"] == 2 and rolled.json()["based_on_id"] == profile["id"]
    history = client.get(P + "/import-profiles", headers=headers["admin"], params={"supplier_id": supplier["id"]}).json()["items"]
    assert {row["status"] for row in history} == {"PUBLISHED", "RETIRED"}


def test_manual_ingestion_deduplicates_and_rejects_bad_rows(env):
    client, headers, _, _ = env
    supplier = create_supplier(client, headers)
    profile = create_profile(client, headers, supplier)
    source = client.post(P + "/ingestion-sources", headers=headers["operator"], json={"name": "页面收件", "kind": "MANUAL", "supplier_id": supplier["id"], "location": "", "config": {}}).json()
    first = client.post(P + f"/ingestion-sources/{source['id']}/upload", headers=headers["operator"], params={"profile_id": profile["id"], "external_key": "manual-1"}, files={"file": ("goods.csv", CSV, "text/csv")})
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "READY" and first.json()["batch_id"] and not first.json()["duplicate"]
    second = client.post(P + f"/ingestion-sources/{source['id']}/upload", headers=headers["operator"], params={"profile_id": profile["id"], "external_key": "manual-2"}, files={"file": ("copy.csv", CSV, "text/csv")})
    assert second.status_code == 201 and second.json()["id"] == first.json()["id"] and second.json()["duplicate"]
    with transaction() as s:
        assert len(s.scalars(select(IngestionEvent).where(IngestionEvent.source_id == source["id"])).all()) == 1

    bad_profile = create_profile(client, headers, supplier, {"sku": "商品编号", "name": "商品名称"})
    bad_source = client.post(P + "/ingestion-sources", headers=headers["operator"], json={"name": "错误文件收件", "kind": "MANUAL", "supplier_id": supplier["id"], "location": "", "config": {}}).json()
    rejected = client.post(P + f"/ingestion-sources/{bad_source['id']}/upload", headers=headers["operator"], params={"profile_id": bad_profile["id"], "external_key": "bad-1"}, files={"file": ("bad.csv", BAD_CSV, "text/csv")})
    assert rejected.status_code == 201 and rejected.json()["status"] == "REJECTED" and not rejected.json()["batch_id"]
    report = client.get(P + f"/ingestion-events/{rejected.json()['id']}/errors", headers=headers["operator"])
    assert report.status_code == 200 and "商品名称为空" in report.content.decode("utf-8-sig")
    replay = client.post(P + f"/ingestion-events/{rejected.json()['id']}/replay", headers=headers["operator"], json={"reason": "尝试绕过行级错误"})
    assert replay.status_code == 409 and replay.json()["code"] == "INGESTION_REPLAY_STATE"


def test_signed_api_ingestion_and_cross_tenant_boundary(env):
    client, headers, _, _ = env
    supplier = create_supplier(client, headers)
    profile = create_profile(client, headers, supplier)
    source_response = client.post(P + "/ingestion-sources", headers=headers["admin"], json={"name": "受控 API", "kind": "API", "supplier_id": supplier["id"], "location": "", "config": {}})
    source, secret = source_response.json(), source_response.json()["signing_secret"]
    with transaction(write=True) as s:
        org = headers["admin"]["X-Organization-ID"]
        member = s.scalar(select(Membership).where(Membership.org_id == org, Membership.user_id == source["created_by"]))
        member.roles = sorted(set(member.roles + ["integration_manager"]))
    account = client.post(P + "/service-accounts", headers=headers["admin"], json={"name": "供应商 API", "scopes": ["ingestion:write"], "expires_in_days": 30})
    assert account.status_code == 201, account.text
    token = account.json()["credential"]
    stamp, event_id = str(int(time.time())), "supplier-event-001"
    signature = hmac.new(secret.encode(), f"{stamp}.{event_id}.".encode() + CSV, hashlib.sha256).hexdigest()
    machine_headers = {"Authorization": "Bearer " + token, "X-Organization-ID": headers["admin"]["X-Organization-ID"], "X-Event-ID": event_id, "X-Timestamp": stamp, "X-Signature": signature, "X-Filename": "api.csv", "X-Profile-ID": profile["id"], "X-Content-SHA256": digest(CSV), "Content-Type": "text/csv"}
    response = client.post(P + f"/service/ingestion-sources/{source['id']}/events", headers=machine_headers, content=CSV)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "READY"
    stale = {**machine_headers, "X-Timestamp": str(int(time.time()) - 600)}
    stale["X-Signature"] = hmac.new(secret.encode(), f"{stale['X-Timestamp']}.other.".encode() + CSV, hashlib.sha256).hexdigest()
    stale["X-Event-ID"] = "other"
    assert client.post(P + f"/service/ingestion-sources/{source['id']}/events", headers=stale, content=CSV).status_code == 401
    with transaction() as s:
        bucket = s.scalar(select(RateBucket).where(RateBucket.org_id == org, RateBucket.principal == account.json()["id"]))
        assert bucket and bucket.count == 1
    assert client.get(P + "/ingestion-sources", headers=headers["other"]).json()["items"] == []


def test_directory_poller_ingests_and_archives_stable_file(env):
    client, headers, _, _ = env
    supplier = create_supplier(client, headers)
    create_profile(client, headers, supplier)
    source = client.post(P + "/ingestion-sources", headers=headers["operator"], json={
        "name": "目录收件", "kind": "DIRECTORY", "supplier_id": supplier["id"],
        "location": f"tests/{supplier['id']}", "config": {"stable_seconds": 1, "archive_after_success": True},
    })
    assert source.status_code == 201, source.text
    inbox = directory_path(source.json()["location"])
    inbox.mkdir(parents=True, exist_ok=True)
    incoming = inbox / "directory-goods.csv"
    incoming.write_bytes(CSV)
    old = time.time() - 2
    os.utime(incoming, (old, old))

    assert poll_once() == 1
    assert not incoming.exists()
    assert (inbox / "archive" / incoming.name).read_bytes() == CSV
    events = client.get(P + "/ingestion-events", headers=headers["operator"]).json()["items"]
    event = next(row for row in events if row["source_id"] == source.json()["id"])
    assert event["status"] == "READY" and event["batch_id"]


def test_risk_queue_and_review_decision_create_versioned_label(env):
    client, headers, _, items = env
    queue = client.get(P + "/learning-queue", headers=headers["reviewer"])
    assert queue.status_code == 200 and queue.json()["items"]
    scores = [row["risk_score"] for row in queue.json()["items"]]
    assert scores == sorted(scores, reverse=True) and all(row["factors"] for row in queue.json()["items"])
    item = next(row for row in items if row["suggestion"] == "RECOMMENDED" and row["candidates"] and not row["candidates"][0]["conflicts"] and not row["candidates"][0]["missing"])
    response = client.post(P + f"/items/{item['id']}/decisions", headers=headers["reviewer"], json={"action": "confirm", "product_id": item["candidates"][0]["product_id"], "expected_version": item["version"], "reason": "已核对型号与关键规格"})
    assert response.status_code == 201, response.text
    labels = client.get(P + "/labels", headers=headers["admin"]).json()["items"]
    label = next(row for row in labels if row["decision_id"] == response.json()["decision_id"])
    assert label["label"] == "MATCH" and label["status"] == "ELIGIBLE" and label["evidence"]["candidate_rank"] == 1


def test_evaluation_and_threshold_admission_create_new_policy(env):
    client, headers, _, _ = env
    org = headers["admin"]["X-Organization-ID"]
    with transaction(write=True) as s:
        dataset = DatasetVersion(id=uid(), org_id=org, name="冻结手机基准", provenance={"source_type": "simulation", "evidence": "deterministic fixture", "entity_grouping_rule": "same entity stays together"}, status="FROZEN", manifest={"version": "v1"}, content_hash=digest({"version": "v1"}))
        s.add(dataset)
    metrics = {"recall_at_20": .99, "top1": .98, "ndcg_at_5": .99, "ece": .03, "brier": .04, "suggestion_precision": .98, "suggestion_precision_ci_lower": .96, "suggestion_coverage": .52, "hard_conflict_suggestions": 0, "inference_p95_ms": 18, "sample_size": 1000}
    evaluation = client.post(P + "/evaluations", headers=headers["admin"], json={"name": "规则与候选对比", "dataset_id": dataset.id, "baseline_name": "v1.3 rules", "scope": "simulation", "config": {"seed": 42}, "metrics": metrics, "slices": {"brand": {}}, "confidence_intervals": {"suggestion_precision": [.96, .99]}, "regressions": []})
    assert evaluation.status_code == 201, evaluation.text
    threshold = client.post(P + "/threshold-policies", headers=headers["admin"], json={"name": "手机审核分流", "evaluation_id": evaluation.json()["id"], "calibration_method": "platt", "thresholds": {"confirm_min": .97, "review_min": .65, "margin_min": .08}})
    assert threshold.status_code == 201
    base = client.get(P + "/policies", headers=headers["admin"]).json()["items"][0]
    approved = client.post(P + f"/threshold-policies/{threshold.json()['id']}/approve", headers=headers["admin"], json={"reason": "冻结基准达到准入门槛", "base_policy_id": base["id"], "activate_default": True})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED" and approved.json()["matching_policy_id"]
