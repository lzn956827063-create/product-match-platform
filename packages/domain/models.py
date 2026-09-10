from sqlalchemy import Boolean, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base, now, uid


class Entity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Tenant(Entity):
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)


def tenant_constraints(*extras):
    return (UniqueConstraint("org_id", "id"), *extras)


def ref(table, field):
    return ForeignKeyConstraint(["org_id", field], [f"{table}.org_id", f"{table}.id"])


class Organization(Entity, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(200))
    dual_review: Mapped[bool] = mapped_column(Boolean, default=True)
    default_policy_id: Mapped[str | None] = mapped_column(String(36))


class User(Entity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Membership(Tenant, Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    roles: Mapped[list] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = tenant_constraints(UniqueConstraint("org_id", "user_id"))


class AuthSession(Entity, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[float] = mapped_column(Float)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginAttempt(Entity, Base):
    __tablename__ = "login_attempts"
    key: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[float] = mapped_column(Float)


class File(Tenant, Base):
    __tablename__ = "files"
    name: Mapped[str] = mapped_column(String(250))
    object_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    encoding: Mapped[str] = mapped_column(String(20), default="utf-8")
    sheets: Mapped[list] = mapped_column(JSON)
    __table_args__ = tenant_constraints()


class Catalog(Tenant, Base):
    __tablename__ = "catalogs"
    name: Mapped[str] = mapped_column(String(200))
    __table_args__ = tenant_constraints()


class CatalogVersion(Tenant, Base):
    __tablename__ = "catalog_versions"
    catalog_id: Mapped[str] = mapped_column(String(36))
    file_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    content_hash: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    mapping: Mapped[dict] = mapped_column(JSON)
    lock_version: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = tenant_constraints(ref("catalogs", "catalog_id"), ref("files", "file_id"), UniqueConstraint("org_id", "catalog_id", "number"))


class Product(Tenant, Base):
    __tablename__ = "catalog_products"
    version_id: Mapped[str] = mapped_column(String(36), index=True)
    sku: Mapped[str] = mapped_column(String(200))
    raw: Mapped[dict] = mapped_column(JSON)
    normalized: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    __table_args__ = tenant_constraints(ref("catalog_versions", "version_id"), UniqueConstraint("org_id", "version_id", "sku"))


class Batch(Tenant, Base):
    __tablename__ = "batches"
    supplier: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = tenant_constraints()


class Revision(Tenant, Base):
    __tablename__ = "batch_revisions"
    batch_id: Mapped[str] = mapped_column(String(36))
    file_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    sheet: Mapped[str] = mapped_column(String(200))
    header_row: Mapped[int] = mapped_column(Integer)
    mapping: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    total: Mapped[int] = mapped_column(Integer)
    valid: Mapped[int] = mapped_column(Integer)
    excluded: Mapped[int] = mapped_column(Integer)
    exclusion_rows: Mapped[list] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    rule_version: Mapped[str] = mapped_column(String(80))
    __table_args__ = tenant_constraints(ref("batches", "batch_id"), ref("files", "file_id"), UniqueConstraint("org_id", "batch_id", "number"))


class Source(Tenant, Base):
    __tablename__ = "source_records"
    revision_id: Mapped[str] = mapped_column(String(36), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(String(200))
    generated_sku: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict] = mapped_column(JSON)
    normalized: Mapped[dict] = mapped_column(JSON)
    issues: Mapped[list] = mapped_column(JSON)
    excluded: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = tenant_constraints(ref("batch_revisions", "revision_id"), UniqueConstraint("org_id", "revision_id", "row_no"))


class Policy(Tenant, Base):
    __tablename__ = "policies"
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints()


class ModelVersion(Entity, Base):
    __tablename__ = "model_versions"
    name: Mapped[str] = mapped_column(String(200))
    artifact_path: Mapped[str] = mapped_column(Text)
    artifact_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(100))
    metrics: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="EXPERIMENTAL")


class Run(Tenant, Base):
    __tablename__ = "match_runs"
    revision_id: Mapped[str] = mapped_column(String(36))
    catalog_version_id: Mapped[str] = mapped_column(String(36))
    policy_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    retry_of: Mapped[str | None] = mapped_column(String(36))
    manifest: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), index=True, default="QUEUED")
    total: Mapped[int] = mapped_column(Integer)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    excluded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = tenant_constraints(ref("batch_revisions", "revision_id"), ref("catalog_versions", "catalog_version_id"), ref("policies", "policy_id"), Index("ix_runs_org_created", "org_id", "created_at", "id"))


class Chunk(Tenant, Base):
    __tablename__ = "run_chunks"
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    number: Mapped[int] = mapped_column(Integer)
    source_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    fence_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_after: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = tenant_constraints(ref("match_runs", "run_id"), UniqueConstraint("org_id", "run_id", "number"))


class Item(Tenant, Base):
    __tablename__ = "match_items"
    run_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str] = mapped_column(String(36))
    suggestion: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    current_decision_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = tenant_constraints(ref("match_runs", "run_id"), ref("source_records", "source_id"), UniqueConstraint("org_id", "run_id", "source_id"), Index("ix_items_filter", "org_id", "run_id", "suggestion", "id"), Index("ix_items_run_page", "org_id", "run_id", "id"), Index("ix_items_status_page", "org_id", "run_id", "status", "id"))


class Candidate(Tenant, Base):
    __tablename__ = "candidates"
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[str] = mapped_column(String(36))
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    features: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON)
    conflicts: Mapped[list] = mapped_column(JSON)
    missing: Mapped[list] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref("match_items", "item_id"), ref("catalog_products", "product_id"), UniqueConstraint("org_id", "item_id", "product_id"))


class ReviewEvent(Tenant, Base):
    __tablename__ = "review_events"
    item_id: Mapped[str] = mapped_column(String(36))
    seq: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(30))
    product_id: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    selection_source: Mapped[str] = mapped_column(String(30))
    __table_args__ = tenant_constraints(ref("match_items", "item_id"), ref("catalog_products", "product_id"), UniqueConstraint("org_id", "item_id", "seq"))


class Mapping(Tenant, Base):
    __tablename__ = "mappings"
    item_id: Mapped[str] = mapped_column(String(36))
    decision_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str] = mapped_column(String(36))
    product_id: Mapped[str] = mapped_column(String(36))
    valid_to: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = tenant_constraints(ref("match_items", "item_id"), ref("review_events", "decision_id"), ref("source_records", "source_id"), ref("catalog_products", "product_id"), Index("uq_active_mapping", "org_id", "item_id", unique=True, sqlite_where=text("valid_to IS NULL"), postgresql_where=text("valid_to IS NULL")))


class Export(Tenant, Base):
    __tablename__ = "exports"
    run_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(30))
    format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    count: Mapped[int] = mapped_column(Integer)
    filters: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str | None] = mapped_column(Text)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = tenant_constraints(ref("match_runs", "run_id"))


class ExportRow(Tenant, Base):
    __tablename__ = "export_rows"
    export_id: Mapped[str] = mapped_column(String(36))
    item_id: Mapped[str] = mapped_column(String(36))
    item_version: Mapped[int] = mapped_column(Integer)
    row_no: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref("exports", "export_id"), ref("match_items", "item_id"), UniqueConstraint("org_id", "export_id", "row_no"))


class Audit(Tenant, Base):
    __tablename__ = "audit_logs"
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(36))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    request_id: Mapped[str] = mapped_column(String(100))
    __table_args__ = tenant_constraints()


class Outbox(Tenant, Base):
    __tablename__ = "outbox"
    event_key: Mapped[str] = mapped_column(String(150), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[str] = mapped_column(String(36))
    published_at: Mapped[float | None] = mapped_column(Float)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = tenant_constraints()


class Idempotency(Tenant, Base):
    __tablename__ = "idempotency_keys"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    route: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(UniqueConstraint("org_id", "user_id", "route", "key"))


class Scheduler(Base):
    __tablename__ = "scheduler_locks"
    id: Mapped[str] = mapped_column(String(30), primary_key=True)


class ImportJob(Tenant, Base):
    __tablename__ = "import_jobs"
    file_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20))
    cache_key: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="UPLOADED")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    result_key: Mapped[str | None] = mapped_column(Text)
    result_hash: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    fence_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = tenant_constraints(ref("files", "file_id"), UniqueConstraint("org_id", "kind", "cache_key"))


class MappingTemplate(Tenant, Base):
    __tablename__ = "mapping_templates"
    supplier: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(200))
    number: Mapped[int] = mapped_column(Integer)
    headers: Mapped[list] = mapped_column(JSON)
    mapping: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = tenant_constraints(UniqueConstraint("org_id", "supplier", "name", "number"))


class CorrectionDraft(Tenant, Base):
    __tablename__ = "correction_drafts"
    run_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    fields: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    submitted_revision_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = tenant_constraints(ref("match_runs", "run_id"), ref("source_records", "source_id"), ref("batch_revisions", "submitted_revision_id"), UniqueConstraint("org_id", "run_id", "source_id", "actor_id"))


class RevisionLineage(Tenant, Base):
    __tablename__ = "revision_lineage"
    revision_id: Mapped[str] = mapped_column(String(36))
    parent_revision_id: Mapped[str] = mapped_column(String(36))
    parent_run_id: Mapped[str] = mapped_column(String(36))
    source_links: Mapped[dict] = mapped_column(JSON)
    mode: Mapped[str] = mapped_column(String(30), default="correction_subset")
    __table_args__ = tenant_constraints(ref("batch_revisions", "revision_id"), ref("batch_revisions", "parent_revision_id"), ref("match_runs", "parent_run_id"), UniqueConstraint("org_id", "revision_id"))


class PolicyEvaluation(Tenant, Base):
    __tablename__ = "policy_evaluations"
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSON)
    report: Mapped[dict] = mapped_column(JSON)
    report_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = tenant_constraints()


class ShadowRun(Tenant, Base):
    __tablename__ = "shadow_runs"
    run_id: Mapped[str] = mapped_column(String(36))
    evaluation_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = tenant_constraints(ref("match_runs", "run_id"), ref("policy_evaluations", "evaluation_id"))


class ArtifactDeletion(Tenant, Base):
    __tablename__ = "artifact_deletions"
    export_id: Mapped[str] = mapped_column(String(36))
    deleted_at: Mapped[str | None] = mapped_column(String(40))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = tenant_constraints(ref("exports", "export_id"), UniqueConstraint("org_id", "export_id"))


class UsageEvent(Tenant, Base):
    __tablename__ = "usage_events"
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    run_id: Mapped[str] = mapped_column(String(36))
    item_id: Mapped[str | None] = mapped_column(String(36))
    event: Mapped[str] = mapped_column(String(40))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    trial_id: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = tenant_constraints(ref("match_runs", "run_id"), ref("match_items", "item_id"))
