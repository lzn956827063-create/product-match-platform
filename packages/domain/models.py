from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint, text
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
    search_name: Mapped[str] = mapped_column(Text, default='', server_default='')
    search_model: Mapped[str] = mapped_column(String(300), default='', server_default='')
    __table_args__ = tenant_constraints(ref("catalog_versions", "version_id"), UniqueConstraint("org_id", "version_id", "sku"), Index('ix_product_model_prefix','org_id','version_id','search_model','id'))


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


# Enterprise trial additions are separate tables so v1.1 history stays intact.
class BatchResultState(Tenant, Base):
    __tablename__ = 'batch_result_states'
    batch_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = tenant_constraints(ref('batches', 'batch_id'), UniqueConstraint('org_id', 'batch_id'))


class SourceIdentity(Tenant, Base):
    __tablename__ = 'source_identities'
    batch_id: Mapped[str] = mapped_column(String(36))
    source_id: Mapped[str] = mapped_column(String(36))
    stable_key: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(ref('batches', 'batch_id'), ref('source_records', 'source_id'), UniqueConstraint('org_id', 'source_id'))


class BatchRelease(Tenant, Base):
    __tablename__ = 'batch_releases'
    batch_id: Mapped[str] = mapped_column(String(36))
    baseline_run_id: Mapped[str] = mapped_column(String(36))
    base_release_id: Mapped[str | None] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='DRAFT')
    config: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=0)
    result_version: Mapped[int | None] = mapped_column(Integer)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    invalidated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    published_at: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = tenant_constraints(ref('batches', 'batch_id'), ref('match_runs', 'baseline_run_id'), ref('batch_releases', 'base_release_id'), UniqueConstraint('org_id', 'batch_id', 'number'), Index('uq_current_batch_release','org_id','batch_id',unique=True,sqlite_where=text("status = 'PUBLISHED'"),postgresql_where=text("status = 'PUBLISHED'")))


class ReleaseRow(Tenant, Base):
    __tablename__ = 'release_rows'
    release_id: Mapped[str] = mapped_column(String(36))
    stable_key: Mapped[str] = mapped_column(String(200))
    source_id: Mapped[str] = mapped_column(String(36))
    item_id: Mapped[str | None] = mapped_column(String(36))
    decision_id: Mapped[str | None] = mapped_column(String(36))
    product_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('batch_releases', 'release_id'), ref('source_records', 'source_id'), ref('match_items', 'item_id'), ref('review_events', 'decision_id'), ref('catalog_products', 'product_id'), UniqueConstraint('org_id', 'release_id', 'stable_key'), Index('ix_release_rows_product', 'org_id', 'product_id'), Index('ix_release_rows_item', 'org_id', 'item_id'))


class ReleaseEvent(Tenant, Base):
    __tablename__ = 'release_events'
    release_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    detail: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('batch_releases', 'release_id'))


class ReleaseArtifact(Tenant, Base):
    __tablename__ = 'release_artifacts'
    release_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default='QUEUED')
    count: Mapped[int] = mapped_column(Integer, default=0)
    object_key: Mapped[str | None] = mapped_column(Text)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = tenant_constraints(ref('batch_releases', 'release_id'), UniqueConstraint('org_id', 'release_id', 'kind'))


class WorkflowSettings(Tenant, Base):
    __tablename__ = 'workflow_settings'
    require_claim: Mapped[bool] = mapped_column(Boolean, default=False)
    claim_seconds: Mapped[int] = mapped_column(Integer, default=300)
    queue_limit: Mapped[int] = mapped_column(Integer, default=20)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, default=1073741824)
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=1200)
    __table_args__ = tenant_constraints(UniqueConstraint('org_id'))


class ReviewAssignment(Tenant, Base):
    __tablename__ = 'review_assignments'
    item_id: Mapped[str] = mapped_column(String(36))
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    assigned_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    reason: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    __table_args__ = tenant_constraints(ref('match_items', 'item_id'), UniqueConstraint('org_id', 'item_id'))


class ReviewClaim(Tenant, Base):
    __tablename__ = 'review_claims'
    item_id: Mapped[str] = mapped_column(String(36))
    holder_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(30), default='AVAILABLE')
    __table_args__ = tenant_constraints(ref('match_items', 'item_id'), UniqueConstraint('org_id', 'item_id'))


class CoordinationEvent(Tenant, Base):
    __tablename__ = 'coordination_events'
    item_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(30))
    detail: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('match_items', 'item_id'))


class Supplier(Tenant, Base):
    __tablename__ = 'suppliers'
    name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = tenant_constraints()


class BatchSupplier(Tenant, Base):
    __tablename__ = 'batch_suppliers'
    batch_id: Mapped[str] = mapped_column(String(36))
    supplier_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    reason: Mapped[str] = mapped_column(Text)
    __table_args__ = tenant_constraints(ref('batches', 'batch_id'), ref('suppliers', 'supplier_id'), UniqueConstraint('org_id', 'batch_id'))


class TemplateSupplier(Tenant, Base):
    __tablename__ = 'template_suppliers'
    template_id: Mapped[str] = mapped_column(String(36))
    supplier_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = tenant_constraints(ref('mapping_templates', 'template_id'), ref('suppliers', 'supplier_id'), UniqueConstraint('org_id', 'template_id'))


class CatalogIdentity(Tenant, Base):
    __tablename__ = 'catalog_identities'
    catalog_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = tenant_constraints(ref('catalogs', 'catalog_id'))


class ProductIdentity(Tenant, Base):
    __tablename__ = 'product_identities'
    product_id: Mapped[str] = mapped_column(String(36))
    identity_id: Mapped[str] = mapped_column(String(36))
    version_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = tenant_constraints(ref('catalog_products', 'product_id'), ref('catalog_identities', 'identity_id'), ref('catalog_versions', 'version_id'), UniqueConstraint('org_id', 'product_id'), UniqueConstraint('org_id', 'identity_id', 'version_id'))


class CatalogChange(Tenant, Base):
    __tablename__ = 'catalog_changes'
    catalog_id: Mapped[str] = mapped_column(String(36))
    from_version_id: Mapped[str] = mapped_column(String(36))
    to_version_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default='QUEUED')
    links: Mapped[dict] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64), default='', server_default='')
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(ref('catalogs', 'catalog_id'), ref('catalog_versions', 'from_version_id'), ref('catalog_versions', 'to_version_id'), UniqueConstraint('org_id', 'from_version_id', 'to_version_id'))


class CatalogActivation(Tenant, Base):
    __tablename__ = 'catalog_activations'
    catalog_id: Mapped[str] = mapped_column(String(36))
    version_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = tenant_constraints(ref('catalogs', 'catalog_id'), ref('catalog_versions', 'version_id'), UniqueConstraint('org_id', 'catalog_id'))


class ImpactTask(Tenant, Base):
    __tablename__ = 'impact_tasks'
    change_id: Mapped[str] = mapped_column(String(36))
    product_id: Mapped[str] = mapped_column(String(36))
    item_id: Mapped[str | None] = mapped_column(String(36))
    release_id: Mapped[str | None] = mapped_column(String(36))
    resource_key: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30), default='OPEN')
    resolution: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    __table_args__ = tenant_constraints(ref('catalog_changes', 'change_id'), ref('catalog_products', 'product_id'), ref('match_items', 'item_id'), ref('batch_releases', 'release_id'), UniqueConstraint('org_id', 'change_id', 'resource_key'))


class ServiceAccount(Tenant, Base):
    __tablename__ = 'service_accounts'
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = tenant_constraints()


class ServiceAccess(Tenant, Base):
    __tablename__ = 'service_access'
    account_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(120))
    resource_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = tenant_constraints(ref('service_accounts', 'account_id'))


class Integration(Tenant, Base):
    __tablename__ = 'integrations'
    name: Mapped[str] = mapped_column(String(200))
    account_id: Mapped[str] = mapped_column(String(36))
    url: Mapped[str] = mapped_column(Text)
    secret_cipher: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    receipt_timeout_seconds: Mapped[int] = mapped_column(Integer, default=900, server_default='900')
    receipt_query_url: Mapped[str | None] = mapped_column(Text)
    reconciliation_owner_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(ref('service_accounts', 'account_id'))


class Delivery(Tenant, Base):
    __tablename__ = 'deliveries'
    release_id: Mapped[str] = mapped_column(String(36))
    integration_id: Mapped[str] = mapped_column(String(36))
    event_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default='QUEUED')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry: Mapped[float] = mapped_column(Float, default=0)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    fence_token: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[float | None] = mapped_column(Float)
    receipt_due_at: Mapped[float | None] = mapped_column(Float, index=True)
    last_query_at: Mapped[float | None] = mapped_column(Float)
    escalation_state: Mapped[str] = mapped_column(String(30), default='NONE', server_default='NONE')
    __table_args__ = tenant_constraints(ref('batch_releases', 'release_id'), ref('integrations', 'integration_id'), UniqueConstraint('org_id', 'integration_id', 'event_id'))


class DeliveryAttempt(Tenant, Base):
    __tablename__ = 'delivery_attempts'
    delivery_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = tenant_constraints(ref('deliveries', 'delivery_id'), UniqueConstraint('org_id', 'delivery_id', 'number'))


class DatasetVersion(Tenant, Base):
    __tablename__ = 'dataset_versions'
    name: Mapped[str] = mapped_column(String(200))
    provenance: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default='DRAFT')
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = tenant_constraints()


class AnnotationTask(Tenant, Base):
    __tablename__ = 'annotation_tasks'
    dataset_id: Mapped[str] = mapped_column(String(36))
    item_id: Mapped[str] = mapped_column(String(36))
    product_id: Mapped[str] = mapped_column(String(36))
    entity_key: Mapped[str] = mapped_column(String(200))
    partition: Mapped[str] = mapped_column(String(20))
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default='OPEN')
    label: Mapped[str | None] = mapped_column(String(30))
    __table_args__ = tenant_constraints(ref('dataset_versions', 'dataset_id'), ref('match_items', 'item_id'), ref('catalog_products', 'product_id'), UniqueConstraint('org_id', 'dataset_id', 'item_id', 'product_id'))


class AnnotationDecision(Tenant, Base):
    __tablename__ = 'annotation_decisions'
    task_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    seconds: Mapped[int] = mapped_column(Integer)
    __table_args__ = tenant_constraints(ref('annotation_tasks', 'task_id'), UniqueConstraint('org_id', 'task_id', 'actor_id'))


class SamplingRound(Tenant, Base):
    __tablename__ = 'sampling_rounds'
    dataset_id: Mapped[str] = mapped_column(String(36))
    strategy: Mapped[str] = mapped_column(String(30))
    number: Mapped[int] = mapped_column(Integer)
    seed: Mapped[int] = mapped_column(Integer)
    task_ids: Mapped[list] = mapped_column(JSON)
    budget: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('dataset_versions', 'dataset_id'), UniqueConstraint('org_id', 'dataset_id', 'strategy', 'seed', 'number'))


class QuotaReservation(Tenant, Base):
    __tablename__ = 'quota_reservations'
    resource_key: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(30))
    amount: Mapped[int] = mapped_column(BigInteger)
    released: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = tenant_constraints(UniqueConstraint('org_id', 'resource_key'))


class RateBucket(Tenant, Base):
    __tablename__ = 'rate_buckets'
    principal: Mapped[str] = mapped_column(String(80))
    window: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = tenant_constraints(UniqueConstraint('org_id', 'principal', 'window'))


class FairTurn(Tenant, Base):
    __tablename__ = 'fair_turns'
    last_started: Mapped[str] = mapped_column(String(40), default='')
    __table_args__ = tenant_constraints(UniqueConstraint('org_id'))


class CatalogPlan(Tenant, Base):
    __tablename__ = 'catalog_plans'
    from_version_id: Mapped[str] = mapped_column(String(36))
    to_version_id: Mapped[str] = mapped_column(String(36))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default='QUEUED')
    version: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(ref('catalog_versions','from_version_id'),ref('catalog_versions','to_version_id'))


class CatalogPlanRow(Tenant, Base):
    __tablename__ = 'catalog_plan_rows'
    plan_id: Mapped[str] = mapped_column(String(36))
    product_id: Mapped[str] = mapped_column(String(36))
    proposed_id: Mapped[str | None] = mapped_column(String(36))
    group: Mapped[str] = mapped_column(String(30))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default='')
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(ref('catalog_plans','plan_id'),ref('catalog_products','product_id'),ref('catalog_products','proposed_id'),UniqueConstraint('org_id','plan_id','product_id'),Index('ix_plan_row_page','org_id','plan_id','group','id'))


class CatalogChangeRow(Tenant, Base):
    __tablename__ = 'catalog_change_rows'
    change_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('catalog_changes','change_id'),UniqueConstraint('org_id','change_id','number'))


class ReleaseDelta(Tenant, Base):
    __tablename__ = 'release_deltas'
    release_id: Mapped[str] = mapped_column(String(36))
    base_release_id: Mapped[str | None] = mapped_column(String(36))
    algorithm: Mapped[str] = mapped_column(String(30), default='delta-v1')
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer)
    __table_args__ = tenant_constraints(ref('batch_releases','release_id'),ref('batch_releases','base_release_id'),UniqueConstraint('org_id','release_id','algorithm'))


class ReleaseDeltaRow(Tenant, Base):
    __tablename__ = 'release_delta_rows'
    delta_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('release_deltas','delta_id'),UniqueConstraint('org_id','delta_id','number'))


class QualitySnapshot(Tenant, Base):
    __tablename__ = 'quality_snapshots'
    revision_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('batch_revisions','revision_id'),UniqueConstraint('org_id','revision_id'))


class QualityIssue(Tenant, Base):
    __tablename__ = 'quality_issues'
    snapshot_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = tenant_constraints(ref('quality_snapshots','snapshot_id'),UniqueConstraint('org_id','snapshot_id','number'))


class TodoAssignment(Tenant, Base):
    __tablename__ = 'todo_assignments'
    resource_key: Mapped[str] = mapped_column(String(100))
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    actor_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    reason: Mapped[str] = mapped_column(Text)
    __table_args__ = tenant_constraints(UniqueConstraint('org_id','resource_key'))


class IngestionSource(Tenant, Base):
    __tablename__ = 'ingestion_sources'
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    supplier_id: Mapped[str] = mapped_column(String(36))
    location: Mapped[str] = mapped_column(Text, default='')
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_cipher: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default='ACTIVE')
    last_checked_at: Mapped[str | None] = mapped_column(String(40))
    last_success_at: Mapped[str | None] = mapped_column(String(40))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(
        ref('suppliers', 'supplier_id'),
        UniqueConstraint('org_id', 'name'),
        Index('ix_ingestion_source_status', 'org_id', 'status', 'kind'),
    )


class ImportProfile(Tenant, Base):
    __tablename__ = 'import_profiles'
    supplier_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(200))
    number: Mapped[int] = mapped_column(Integer)
    file_type: Mapped[str] = mapped_column(String(20))
    sheet: Mapped[str] = mapped_column(String(200), default='CSV')
    header_row: Mapped[int] = mapped_column(Integer, default=1)
    encoding: Mapped[str] = mapped_column(String(20), default='utf-8')
    mapping: Mapped[dict] = mapped_column(JSON)
    transformations: Mapped[dict] = mapped_column(JSON, default=dict)
    validations: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='DRAFT')
    lock_version: Mapped[int] = mapped_column(Integer, default=0)
    effective_from: Mapped[str | None] = mapped_column(String(40))
    change_note: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    based_on_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = tenant_constraints(
        ref('suppliers', 'supplier_id'),
        ref('import_profiles', 'based_on_id'),
        UniqueConstraint('org_id', 'supplier_id', 'name', 'number'),
        Index('ix_import_profile_active', 'org_id', 'supplier_id', 'status'),
    )


class IngestionEvent(Tenant, Base):
    __tablename__ = 'ingestion_events'
    source_id: Mapped[str] = mapped_column(String(36))
    profile_id: Mapped[str] = mapped_column(String(36))
    external_key: Mapped[str] = mapped_column(String(200))
    file_id: Mapped[str] = mapped_column(String(36))
    object_sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(250))
    status: Mapped[str] = mapped_column(String(30), default='RECEIVED')
    stage: Mapped[str] = mapped_column(String(30), default='VALIDATION')
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    batch_id: Mapped[str | None] = mapped_column(String(36))
    revision_id: Mapped[str | None] = mapped_column(String(36))
    last_error: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(
        ref('ingestion_sources', 'source_id'),
        ref('import_profiles', 'profile_id'),
        ref('files', 'file_id'),
        ref('batches', 'batch_id'),
        ref('batch_revisions', 'revision_id'),
        UniqueConstraint('org_id', 'source_id', 'profile_id', 'object_sha256'),
        UniqueConstraint('org_id', 'source_id', 'external_key'),
        Index('ix_ingestion_event_status', 'org_id', 'status', 'created_at'),
    )


class IngestionAttempt(Tenant, Base):
    __tablename__ = 'ingestion_attempts'
    event_id: Mapped[str] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = tenant_constraints(
        ref('ingestion_events', 'event_id'),
        UniqueConstraint('org_id', 'event_id', 'number'),
    )


class EvaluationRun(Tenant, Base):
    __tablename__ = 'evaluation_runs'
    name: Mapped[str] = mapped_column(String(200))
    dataset_id: Mapped[str] = mapped_column(String(36))
    model_id: Mapped[str | None] = mapped_column(ForeignKey('model_versions.id'))
    baseline_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default='COMPLETED')
    scope: Mapped[str] = mapped_column(String(30))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON)
    slices: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence_intervals: Mapped[dict] = mapped_column(JSON, default=dict)
    regressions: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(
        ref('dataset_versions', 'dataset_id'),
        UniqueConstraint('org_id', 'content_hash'),
    )


class ThresholdPolicy(Tenant, Base):
    __tablename__ = 'threshold_policies'
    name: Mapped[str] = mapped_column(String(200))
    number: Mapped[int] = mapped_column(Integer)
    evaluation_id: Mapped[str] = mapped_column(String(36))
    matching_policy_id: Mapped[str | None] = mapped_column(String(36))
    calibration_method: Mapped[str] = mapped_column(String(30))
    thresholds: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default='DRAFT')
    effective_from: Mapped[str | None] = mapped_column(String(40))
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey('users.id'))
    approval_note: Mapped[str] = mapped_column(Text, default='')
    __table_args__ = tenant_constraints(
        ref('evaluation_runs', 'evaluation_id'),
        ref('policies', 'matching_policy_id'),
        UniqueConstraint('org_id', 'name', 'number'),
    )


class LabelVersion(Tenant, Base):
    __tablename__ = 'label_versions'
    item_id: Mapped[str] = mapped_column(String(36))
    decision_id: Mapped[str] = mapped_column(String(36))
    product_id: Mapped[str | None] = mapped_column(String(36))
    number: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(30))
    error_type: Mapped[str | None] = mapped_column(String(50))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30))
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))
    __table_args__ = tenant_constraints(
        ref('match_items', 'item_id'),
        ref('review_events', 'decision_id'),
        ref('catalog_products', 'product_id'),
        UniqueConstraint('org_id', 'decision_id'),
        UniqueConstraint('org_id', 'item_id', 'number'),
        Index('ix_label_training_status', 'org_id', 'status', 'error_type'),
    )
