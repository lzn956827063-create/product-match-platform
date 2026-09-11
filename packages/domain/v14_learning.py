"""Explainable review risk and immutable labels derived from review decisions."""
from sqlalchemy import func, select

from .db import uid
from .errors import require
from .models import Candidate, ImpactTask, Item, LabelVersion, ReviewEvent, Source


ERROR_TYPES = {
    "candidate_not_recalled",
    "ranking_error",
    "catalog_missing",
    "normalization_error",
    "key_field_conflict",
    "insufficient_source",
    "duplicate_product",
    "annotation_dispute",
}


def risk(s, org_id, item):
    candidates = list(s.scalars(select(Candidate).where(Candidate.org_id == org_id, Candidate.item_id == item.id).order_by(Candidate.rank).limit(5)))
    factors, score = [], 0.0
    top = candidates[0] if candidates else None
    second = candidates[1] if len(candidates) > 1 else None
    if top and top.conflicts:
        score += 100
        factors.append({"code": "HARD_CONFLICT", "label": "关键字段硬冲突", "detail": "、".join(top.conflicts), "weight": 100})
    if not top:
        score += 95
        factors.append({"code": "NO_CANDIDATE", "label": "候选未召回", "detail": "需要人工搜索标准库", "weight": 95})
    if top and top.missing:
        score += 45
        factors.append({"code": "MISSING_FIELDS", "label": "关键字段缺失", "detail": "、".join(top.missing), "weight": 45})
    margin = (top.score - second.score) if top and second else (top.score if top else 0)
    if top and margin < 0.08:
        weight = round((0.08 - margin) / 0.08 * 35, 2)
        score += weight
        factors.append({"code": "SMALL_MARGIN", "label": "前两名候选接近", "detail": f"分差 {margin:.4f}", "weight": weight})
    if top:
        uncertainty = round((1 - max(0, min(1, top.score))) * 30, 2)
        if uncertainty >= 6:
            score += uncertainty
            factors.append({"code": "LOW_CONFIDENCE", "label": "候选不确定性较高", "detail": f"排序分 {top.score:.4f}，不是准确率", "weight": uncertainty})
    impacted = s.scalar(select(func.count()).select_from(ImpactTask).where(ImpactTask.org_id == org_id, ImpactTask.item_id == item.id, ImpactTask.status == "OPEN")) or 0
    if impacted:
        score += 50
        factors.append({"code": "RELEASE_IMPACT", "label": "影响已发布结果", "detail": f"{impacted} 个待处置影响", "weight": 50})
    level = "CRITICAL" if score >= 100 else "HIGH" if score >= 55 else "NORMAL"
    if not factors:
        factors.append({"code": "STABLE_SUGGESTION", "label": "候选稳定，仍需人工确认", "detail": "未发现硬冲突或明显不确定因素", "weight": 0})
    return {
        "risk_score": round(score, 2),
        "risk_level": level,
        "factors": factors,
        "candidate_margin": round(margin, 4) if top else None,
        "top_score": round(top.score, 4) if top else None,
        "rejected": not top or bool(top.conflicts),
    }


def sync_label(s, ctx, item, event):
    old = s.scalar(select(LabelVersion).where(LabelVersion.org_id == ctx.org_id, LabelVersion.decision_id == event.id))
    if old:
        return old
    number = (s.scalar(select(func.max(LabelVersion.number)).where(LabelVersion.org_id == ctx.org_id, LabelVersion.item_id == item.id)) or 0) + 1
    candidate = s.scalar(select(Candidate).where(Candidate.org_id == ctx.org_id, Candidate.item_id == item.id, Candidate.product_id == event.product_id)) if event.product_id else None
    label = {"confirm": "MATCH", "unmatched": "NO_MATCH", "needs_info": "INSUFFICIENT", "revoke": "REVOKED"}[event.action]
    status = "HOLD" if event.action == "needs_info" else "RETIRED" if event.action == "revoke" else "ELIGIBLE"
    row = LabelVersion(
        id=uid(),
        org_id=ctx.org_id,
        item_id=item.id,
        decision_id=event.id,
        product_id=event.product_id,
        number=number,
        label=label,
        evidence={
            "review_reason": event.reason,
            "selection_source": event.selection_source,
            "candidate_rank": candidate.rank if candidate else None,
            "candidate_score": candidate.score if candidate else None,
            "conflicts": candidate.conflicts if candidate else [],
            "missing": candidate.missing if candidate else [],
        },
        status=status,
        created_by=ctx.user_id,
    )
    s.add(row)
    return row


def classify_label(label, error_type, status):
    require(error_type in ERROR_TYPES, 422, "LABEL_ERROR_TYPE", "不支持此误差类型")
    require(status in {"ELIGIBLE", "DISPUTED", "HOLD"}, 422, "LABEL_STATUS", "不支持此标签状态")
    if label.label == "INSUFFICIENT":
        require(status != "ELIGIBLE", 409, "LABEL_NOT_FINAL", "退回补资料在形成最终决定前不能进入训练数据")
    if status == "DISPUTED":
        error_type = "annotation_dispute"
    label.error_type, label.status = error_type, status
    return label


def similar_history(s, org_id, item, limit=5):
    source = s.get(Source, item.source_id)
    if not source:
        return []
    brand, model = source.normalized.get("brand"), source.normalized.get("model")
    rows = s.execute(
        select(LabelVersion, Source)
        .join(Item, (Item.org_id == LabelVersion.org_id) & (Item.id == LabelVersion.item_id))
        .join(Source, (Source.org_id == Item.org_id) & (Source.id == Item.source_id))
        .where(LabelVersion.org_id == org_id, LabelVersion.status == "ELIGIBLE", LabelVersion.item_id != item.id)
        .order_by(LabelVersion.created_at.desc())
        .limit(100)
    )
    matches = []
    for label, prior in rows:
        if (brand and prior.normalized.get("brand") == brand) or (model and prior.normalized.get("model") == model):
            matches.append({"label": label.label, "error_type": label.error_type, "source_name": prior.normalized.get("name"), "created_at": label.created_at})
        if len(matches) >= limit:
            break
    return matches
