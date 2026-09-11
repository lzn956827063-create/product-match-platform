"""Register v1.4 demo configuration and measured simulation evidence idempotently."""
import json
from pathlib import Path

from sqlalchemy import select

from packages.domain.db import now, transaction, uid
from packages.domain.models import (
    Batch,
    BatchSupplier,
    DatasetVersion,
    EvaluationRun,
    ImportProfile,
    IngestionSource,
    Membership,
    Organization,
    Supplier,
    ThresholdPolicy,
    User,
)
from packages.domain.v14_ingestion import directory_path
from packages.matching.normalize import digest
from scripts.seed import MAPPING


def main():
    root = Path(__file__).resolve().parents[1]
    dataset_path = root / "ml/datasets/phone-sim-v1/manifest.json"
    report_path = root / "docs/optimization/phone-experiment.json"
    dataset_manifest = json.loads(dataset_path.read_text())
    report = json.loads(report_path.read_text())
    measured = report["lightgbm"]["metrics"]
    calibration = report["calibration"]["calibrated"]
    created = 0
    with transaction(write=True) as s:
        for org in s.scalars(select(Organization)):
            admin = s.scalar(select(User).join(Membership, Membership.user_id == User.id).where(Membership.org_id == org.id, User.email.like("admin@%")))
            if not admin:
                continue
            batch = s.scalar(select(Batch).where(Batch.org_id == org.id).order_by(Batch.created_at).limit(1))
            supplier_name = batch.supplier if batch else "演示文件供应商"
            supplier = s.scalar(select(Supplier).where(Supplier.org_id == org.id, Supplier.name == supplier_name))
            if not supplier:
                supplier = Supplier(id=uid(), org_id=org.id, name=supplier_name, aliases=[])
                s.add(supplier); s.flush(); created += 1
            if batch and not s.scalar(select(BatchSupplier).where(BatchSupplier.org_id == org.id, BatchSupplier.batch_id == batch.id)):
                s.add(BatchSupplier(org_id=org.id, batch_id=batch.id, supplier_id=supplier.id, actor_id=admin.id, reason="登记 v1.4 演示供应商"))
            profile = s.scalar(select(ImportProfile).where(ImportProfile.org_id == org.id, ImportProfile.supplier_id == supplier.id, ImportProfile.name == "手机商品标准模板", ImportProfile.status == "PUBLISHED"))
            if not profile:
                profile = ImportProfile(
                    id=uid(), org_id=org.id, supplier_id=supplier.id, name="手机商品标准模板", number=1,
                    file_type="csv", sheet="CSV", header_row=1, encoding="utf-8", mapping=MAPPING,
                    transformations={"brand": {"trim": True}, "model": {"trim": True, "case": "lower"}},
                    validations={"required": ["name", "brand", "model"], "unique": ["sku"]},
                    status="PUBLISHED", lock_version=1, effective_from=now(), change_note="演示构造数据字段模板",
                    created_by=admin.id, approved_by=admin.id,
                )
                s.add(profile); s.flush(); created += 1
            for name, kind, location in (("页面文件收件", "MANUAL", ""), ("共享目录收件", "DIRECTORY", f"demo/{org.id[:8]}")):
                if not s.scalar(select(IngestionSource).where(IngestionSource.org_id == org.id, IngestionSource.name == name)):
                    s.add(IngestionSource(org_id=org.id, name=name, kind=kind, supplier_id=supplier.id, location=location, config={"stable_seconds": 10, "archive_after_success": True}, status="ACTIVE", created_by=admin.id))
                    created += 1
                if kind == "DIRECTORY":
                    directory_path(location).mkdir(parents=True, exist_ok=True)
            dataset = s.scalar(select(DatasetVersion).where(DatasetVersion.org_id == org.id, DatasetVersion.name == "手机构造实体基准 v1"))
            if not dataset:
                dataset = DatasetVersion(
                    id=uid(), org_id=org.id, name="手机构造实体基准 v1", status="FROZEN",
                    provenance={"source_type": "simulation", "evidence": "项目内确定性构造数据", "entity_grouping_rule": "同一商品实体及其别名不跨分区", "license": "internal-simulation-only", "split_seed": 42},
                    manifest={"version": dataset_manifest["version"], "catalog": len(dataset_manifest["catalog"]), "queries": len(dataset_manifest["queries"]), "partitions": {key: len(value) for key, value in dataset_manifest["partitions"].items()}, "file_sha256": digest(dataset_path.read_bytes())},
                    content_hash=digest(dataset_manifest),
                )
                s.add(dataset); s.flush(); created += 1
            metrics = {
                "recall_at_20": measured["recall_at_20"], "top1": measured["top1_precision"],
                "ndcg_at_5": measured["ndcg_at_5"], "ece": calibration["ece_10_equal_width_bins"],
                "brier": calibration["brier"], "suggestion_precision": measured["top1_precision"],
                "suggestion_precision_ci_lower": measured["top1_precision_95_ci"][0],
                "suggestion_coverage": measured["coverage"], "hard_conflict_suggestions": measured["hard_conflict_recommendations"],
                "inference_p95_ms": measured["inference_p95_ms"], "sample_size": measured["n"],
                "mrr": measured["mrr"], "rejection_rate": measured["rejection_rate"],
            }
            evaluation_hash = digest({"dataset_hash": dataset.content_hash, "metrics": metrics, "policy": report["lightgbm"]["policy"]})
            evaluation = s.scalar(select(EvaluationRun).where(EvaluationRun.org_id == org.id, EvaluationRun.content_hash == evaluation_hash))
            if not evaluation:
                evaluation = EvaluationRun(
                    id=uid(), org_id=org.id, name="v1.4 两阶段匹配构造基准", dataset_id=dataset.id, model_id=None,
                    baseline_name="v1.3 规则基线", status="COMPLETED", scope="simulation",
                    config={"seed": 42, "candidate_budget": 20, "calibration": "platt", "evidence_file": "docs/optimization/phone-experiment.json"},
                    metrics=metrics, slices=report["lightgbm"]["slices"],
                    confidence_intervals={"suggestion_precision": measured["top1_precision_95_ci"]},
                    regressions=[], content_hash=evaluation_hash, created_by=admin.id,
                )
                s.add(evaluation); s.flush(); created += 1
            if not s.scalar(select(ThresholdPolicy).where(ThresholdPolicy.org_id == org.id, ThresholdPolicy.evaluation_id == evaluation.id)):
                s.add(ThresholdPolicy(org_id=org.id, name="手机审核分流", number=1, evaluation_id=evaluation.id, calibration_method="platt", thresholds={"confirm_min": .98, "review_min": .65, "margin_min": .08}, status="DRAFT", created_by=admin.id, approval_note="构造数据结果仅用于演示，不自动用于正式运行"))
                created += 1
    print(f"v1.4 demo records created={created}; all evaluation evidence is labeled simulation")


if __name__ == "__main__":
    main()
