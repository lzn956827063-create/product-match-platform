BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> d8c4c3aad74d

CREATE TABLE login_attempts (
    key VARCHAR(64) NOT NULL,
    at FLOAT NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX ix_login_attempts_key ON login_attempts (key);

CREATE TABLE model_versions (
    name VARCHAR(200) NOT NULL,
    artifact_path TEXT NOT NULL,
    artifact_hash VARCHAR(64) NOT NULL,
    schema_version VARCHAR(100) NOT NULL,
    metrics JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE organizations (
    name VARCHAR(200) NOT NULL,
    dual_review BOOLEAN NOT NULL,
    default_policy_id VARCHAR(36),
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE scheduler_locks (
    id VARCHAR(30) NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE users (
    email VARCHAR(200) NOT NULL,
    name VARCHAR(100) NOT NULL,
    password_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (email)
);

CREATE TABLE audit_logs (
    actor_id VARCHAR(36) NOT NULL,
    action VARCHAR(80) NOT NULL,
    resource_id VARCHAR(36) NOT NULL,
    detail JSON NOT NULL,
    request_id VARCHAR(100) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_audit_logs_org_id ON audit_logs (org_id);

CREATE TABLE auth_sessions (
    user_id VARCHAR(36) NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    expires_at FLOAT NOT NULL,
    revoked BOOLEAN NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(user_id) REFERENCES users (id),
    UNIQUE (token_hash)
);

CREATE TABLE batches (
    supplier VARCHAR(200) NOT NULL,
    name VARCHAR(200) NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_batches_org_id ON batches (org_id);

CREATE TABLE catalogs (
    name VARCHAR(200) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_catalogs_org_id ON catalogs (org_id);

CREATE TABLE files (
    name VARCHAR(250) NOT NULL,
    object_key TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    size INTEGER NOT NULL,
    encoding VARCHAR(20) NOT NULL,
    sheets JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_files_org_id ON files (org_id);

CREATE TABLE idempotency_keys (
    user_id VARCHAR(36) NOT NULL,
    route VARCHAR(200) NOT NULL,
    key VARCHAR(100) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    FOREIGN KEY(user_id) REFERENCES users (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, user_id, route, key)
);

CREATE INDEX ix_idempotency_keys_org_id ON idempotency_keys (org_id);

CREATE TABLE memberships (
    user_id VARCHAR(36) NOT NULL,
    roles JSON NOT NULL,
    active BOOLEAN NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    FOREIGN KEY(user_id) REFERENCES users (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, user_id)
);

CREATE INDEX ix_memberships_org_id ON memberships (org_id);

CREATE TABLE outbox (
    event_key VARCHAR(150) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    resource_id VARCHAR(36) NOT NULL,
    published_at FLOAT,
    completed BOOLEAN NOT NULL,
    attempts INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (event_key),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_outbox_org_id ON outbox (org_id);

CREATE TABLE policies (
    name VARCHAR(200) NOT NULL,
    config JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_policies_org_id ON policies (org_id);

CREATE TABLE batch_revisions (
    batch_id VARCHAR(36) NOT NULL,
    file_id VARCHAR(36) NOT NULL,
    number INTEGER NOT NULL,
    sheet VARCHAR(200) NOT NULL,
    header_row INTEGER NOT NULL,
    mapping JSON NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    status VARCHAR(30) NOT NULL,
    total INTEGER NOT NULL,
    valid INTEGER NOT NULL,
    excluded INTEGER NOT NULL,
    exclusion_rows JSON NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    rule_version VARCHAR(80) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(org_id, batch_id) REFERENCES batches (org_id, id),
    FOREIGN KEY(org_id, file_id) REFERENCES files (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, batch_id, number),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_batch_revisions_org_id ON batch_revisions (org_id);

CREATE TABLE catalog_versions (
    catalog_id VARCHAR(36) NOT NULL,
    file_id VARCHAR(36) NOT NULL,
    number INTEGER NOT NULL,
    status VARCHAR(30) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    row_count INTEGER NOT NULL,
    errors JSON NOT NULL,
    mapping JSON NOT NULL,
    lock_version INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, catalog_id) REFERENCES catalogs (org_id, id),
    FOREIGN KEY(org_id, file_id) REFERENCES files (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, catalog_id, number),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_catalog_versions_org_id ON catalog_versions (org_id);

CREATE TABLE catalog_products (
    version_id VARCHAR(36) NOT NULL,
    sku VARCHAR(200) NOT NULL,
    raw JSON NOT NULL,
    normalized JSON NOT NULL,
    fingerprint VARCHAR(64) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, version_id) REFERENCES catalog_versions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, version_id, sku)
);

CREATE INDEX ix_catalog_products_org_id ON catalog_products (org_id);

CREATE INDEX ix_catalog_products_version_id ON catalog_products (version_id);

CREATE TABLE match_runs (
    revision_id VARCHAR(36) NOT NULL,
    catalog_version_id VARCHAR(36) NOT NULL,
    policy_id VARCHAR(36) NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    retry_of VARCHAR(36),
    manifest JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    total INTEGER NOT NULL,
    processed INTEGER NOT NULL,
    excluded INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    error TEXT,
    cancel_reason TEXT,
    started_at VARCHAR(40),
    finished_at VARCHAR(40),
    timings JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(org_id, catalog_version_id) REFERENCES catalog_versions (org_id, id),
    FOREIGN KEY(org_id, policy_id) REFERENCES policies (org_id, id),
    FOREIGN KEY(org_id, revision_id) REFERENCES batch_revisions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_match_runs_org_id ON match_runs (org_id);

CREATE INDEX ix_match_runs_status ON match_runs (status);

CREATE INDEX ix_runs_org_created ON match_runs (org_id, created_at, id);

CREATE TABLE source_records (
    revision_id VARCHAR(36) NOT NULL,
    row_no INTEGER NOT NULL,
    sku VARCHAR(200) NOT NULL,
    generated_sku BOOLEAN NOT NULL,
    raw JSON NOT NULL,
    normalized JSON NOT NULL,
    issues JSON NOT NULL,
    excluded BOOLEAN NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, revision_id) REFERENCES batch_revisions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, revision_id, row_no)
);

CREATE INDEX ix_source_records_org_id ON source_records (org_id);

CREATE INDEX ix_source_records_revision_id ON source_records (revision_id);

CREATE TABLE exports (
    run_id VARCHAR(36) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    format VARCHAR(10) NOT NULL,
    status VARCHAR(30) NOT NULL,
    count INTEGER NOT NULL,
    filters JSON NOT NULL,
    snapshot_hash VARCHAR(64) NOT NULL,
    object_key TEXT,
    file_hash VARCHAR(64),
    created_by VARCHAR(36) NOT NULL,
    expires_at FLOAT NOT NULL,
    error TEXT,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(org_id, run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_exports_org_id ON exports (org_id);

CREATE TABLE match_items (
    run_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    suggestion VARCHAR(30) NOT NULL,
    status VARCHAR(30) NOT NULL,
    current_decision_id VARCHAR(36),
    version INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id, source_id) REFERENCES source_records (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, run_id, source_id)
);

CREATE INDEX ix_items_filter ON match_items (org_id, run_id, suggestion, id);

CREATE INDEX ix_match_items_org_id ON match_items (org_id);

CREATE TABLE run_chunks (
    run_id VARCHAR(36) NOT NULL,
    number INTEGER NOT NULL,
    source_ids JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    lease_until FLOAT NOT NULL,
    fence_token INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    retry_after FLOAT NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, run_id, number)
);

CREATE INDEX ix_run_chunks_org_id ON run_chunks (org_id);

CREATE INDEX ix_run_chunks_run_id ON run_chunks (run_id);

CREATE TABLE candidates (
    item_id VARCHAR(36) NOT NULL,
    product_id VARCHAR(36) NOT NULL,
    rank INTEGER NOT NULL,
    score FLOAT NOT NULL,
    features JSON NOT NULL,
    evidence JSON NOT NULL,
    conflicts JSON NOT NULL,
    missing JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id, product_id) REFERENCES catalog_products (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, item_id, product_id)
);

CREATE INDEX ix_candidates_item_id ON candidates (item_id);

CREATE INDEX ix_candidates_org_id ON candidates (org_id);

CREATE TABLE export_rows (
    export_id VARCHAR(36) NOT NULL,
    item_id VARCHAR(36) NOT NULL,
    item_version INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    data JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, export_id) REFERENCES exports (org_id, id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, export_id, row_no),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_export_rows_org_id ON export_rows (org_id);

CREATE TABLE review_events (
    item_id VARCHAR(36) NOT NULL,
    seq INTEGER NOT NULL,
    action VARCHAR(30) NOT NULL,
    product_id VARCHAR(36),
    reason TEXT NOT NULL,
    actor_id VARCHAR(36) NOT NULL,
    selection_source VARCHAR(30) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id, product_id) REFERENCES catalog_products (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, item_id, seq)
);

CREATE INDEX ix_review_events_org_id ON review_events (org_id);

CREATE TABLE mappings (
    item_id VARCHAR(36) NOT NULL,
    decision_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    product_id VARCHAR(36) NOT NULL,
    valid_to VARCHAR(40),
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, decision_id) REFERENCES review_events (org_id, id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id, product_id) REFERENCES catalog_products (org_id, id),
    FOREIGN KEY(org_id, source_id) REFERENCES source_records (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_mappings_org_id ON mappings (org_id);

CREATE UNIQUE INDEX uq_active_mapping ON mappings (org_id, item_id) WHERE valid_to IS NULL;

INSERT INTO alembic_version (version_num) VALUES ('d8c4c3aad74d') RETURNING alembic_version.version_num;

-- Running upgrade d8c4c3aad74d -> f484e73f04f0

CREATE TABLE mapping_templates (
    supplier VARCHAR(200) NOT NULL,
    name VARCHAR(200) NOT NULL,
    number INTEGER NOT NULL,
    headers JSON NOT NULL,
    mapping JSON NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, supplier, name, number)
);

CREATE INDEX ix_mapping_templates_org_id ON mapping_templates (org_id);

CREATE TABLE policy_evaluations (
    name VARCHAR(200) NOT NULL,
    config JSON NOT NULL,
    report JSON NOT NULL,
    report_hash VARCHAR(64) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_policy_evaluations_org_id ON policy_evaluations (org_id);

CREATE TABLE import_jobs (
    file_id VARCHAR(36) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    cache_key VARCHAR(64) NOT NULL,
    config JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    progress INTEGER NOT NULL,
    result_key TEXT,
    result_hash VARCHAR(64),
    summary JSON NOT NULL,
    error TEXT,
    lease_until FLOAT NOT NULL,
    fence_token INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, file_id) REFERENCES files (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, kind, cache_key)
);

CREATE INDEX ix_import_jobs_org_id ON import_jobs (org_id);

CREATE TABLE correction_drafts (
    run_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    actor_id VARCHAR(36) NOT NULL,
    fields JSON NOT NULL,
    evidence TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    submitted_revision_id VARCHAR(36),
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id, source_id) REFERENCES source_records (org_id, id),
    FOREIGN KEY(org_id, submitted_revision_id) REFERENCES batch_revisions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, run_id, source_id, actor_id)
);

CREATE INDEX ix_correction_drafts_org_id ON correction_drafts (org_id);

CREATE TABLE revision_lineage (
    revision_id VARCHAR(36) NOT NULL,
    parent_revision_id VARCHAR(36) NOT NULL,
    parent_run_id VARCHAR(36) NOT NULL,
    source_links JSON NOT NULL,
    mode VARCHAR(30) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, parent_revision_id) REFERENCES batch_revisions (org_id, id),
    FOREIGN KEY(org_id, parent_run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id, revision_id) REFERENCES batch_revisions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, revision_id)
);

CREATE INDEX ix_revision_lineage_org_id ON revision_lineage (org_id);

CREATE TABLE shadow_runs (
    run_id VARCHAR(36) NOT NULL,
    evaluation_id VARCHAR(36) NOT NULL,
    status VARCHAR(30) NOT NULL,
    report JSON NOT NULL,
    error TEXT,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, evaluation_id) REFERENCES policy_evaluations (org_id, id),
    FOREIGN KEY(org_id, run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_shadow_runs_org_id ON shadow_runs (org_id);

CREATE TABLE artifact_deletions (
    export_id VARCHAR(36) NOT NULL,
    deleted_at VARCHAR(40),
    attempts INTEGER NOT NULL,
    error TEXT,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, export_id) REFERENCES exports (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, export_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_artifact_deletions_org_id ON artifact_deletions (org_id);

CREATE TABLE usage_events (
    actor_id VARCHAR(36) NOT NULL,
    run_id VARCHAR(36) NOT NULL,
    item_id VARCHAR(36),
    event VARCHAR(40) NOT NULL,
    duration_ms INTEGER,
    trial_id VARCHAR(100),
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id, run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_usage_events_org_id ON usage_events (org_id);

CREATE INDEX ix_items_run_page ON match_items (org_id, run_id, id);

CREATE INDEX ix_items_status_page ON match_items (org_id, run_id, status, id);

UPDATE alembic_version SET version_num='f484e73f04f0' WHERE alembic_version.version_num = 'd8c4c3aad74d';

-- Running upgrade f484e73f04f0 -> 9b7798dcda5b

CREATE TABLE dataset_versions (
    name VARCHAR(200) NOT NULL,
    provenance JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    manifest JSON NOT NULL,
    content_hash VARCHAR(64),
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_dataset_versions_org_id ON dataset_versions (org_id);

CREATE TABLE fair_turns (
    last_started VARCHAR(40) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_fair_turns_org_id ON fair_turns (org_id);

CREATE TABLE quota_reservations (
    resource_key VARCHAR(150) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    amount BIGINT NOT NULL,
    released BOOLEAN NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, resource_key)
);

CREATE INDEX ix_quota_reservations_org_id ON quota_reservations (org_id);

CREATE TABLE rate_buckets (
    principal VARCHAR(80) NOT NULL,
    "window" INTEGER NOT NULL,
    count INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, principal, "window")
);

CREATE INDEX ix_rate_buckets_org_id ON rate_buckets (org_id);

CREATE TABLE service_accounts (
    name VARCHAR(200) NOT NULL,
    token_hash VARCHAR(64) NOT NULL,
    scopes JSON NOT NULL,
    expires_at FLOAT NOT NULL,
    active BOOLEAN NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_service_accounts_org_id ON service_accounts (org_id);

CREATE TABLE suppliers (
    name VARCHAR(200) NOT NULL,
    aliases JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_suppliers_org_id ON suppliers (org_id);

CREATE TABLE workflow_settings (
    require_claim BOOLEAN NOT NULL,
    claim_seconds INTEGER NOT NULL,
    queue_limit INTEGER NOT NULL,
    storage_bytes BIGINT NOT NULL,
    requests_per_minute INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_workflow_settings_org_id ON workflow_settings (org_id);

CREATE TABLE batch_result_states (
    batch_id VARCHAR(36) NOT NULL,
    version INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, batch_id) REFERENCES batches (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, batch_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_batch_result_states_org_id ON batch_result_states (org_id);

CREATE TABLE batch_suppliers (
    batch_id VARCHAR(36) NOT NULL,
    supplier_id VARCHAR(36) NOT NULL,
    actor_id VARCHAR(36) NOT NULL,
    reason TEXT NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, batch_id) REFERENCES batches (org_id, id),
    FOREIGN KEY(org_id, supplier_id) REFERENCES suppliers (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, batch_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_batch_suppliers_org_id ON batch_suppliers (org_id);

CREATE TABLE catalog_identities (
    catalog_id VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, catalog_id) REFERENCES catalogs (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_catalog_identities_org_id ON catalog_identities (org_id);

CREATE TABLE integrations (
    name VARCHAR(200) NOT NULL,
    account_id VARCHAR(36) NOT NULL,
    url TEXT NOT NULL,
    secret_cipher TEXT NOT NULL,
    active BOOLEAN NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, account_id) REFERENCES service_accounts (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_integrations_org_id ON integrations (org_id);

CREATE TABLE sampling_rounds (
    dataset_id VARCHAR(36) NOT NULL,
    strategy VARCHAR(30) NOT NULL,
    number INTEGER NOT NULL,
    seed INTEGER NOT NULL,
    task_ids JSON NOT NULL,
    budget JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, dataset_id) REFERENCES dataset_versions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, dataset_id, strategy, seed, number),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_sampling_rounds_org_id ON sampling_rounds (org_id);

CREATE TABLE service_access (
    account_id VARCHAR(36) NOT NULL,
    action VARCHAR(120) NOT NULL,
    resource_id VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, account_id) REFERENCES service_accounts (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_service_access_org_id ON service_access (org_id);

CREATE TABLE template_suppliers (
    template_id VARCHAR(36) NOT NULL,
    supplier_id VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, supplier_id) REFERENCES suppliers (org_id, id),
    FOREIGN KEY(org_id, template_id) REFERENCES mapping_templates (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, template_id)
);

CREATE INDEX ix_template_suppliers_org_id ON template_suppliers (org_id);

CREATE TABLE catalog_activations (
    catalog_id VARCHAR(36) NOT NULL,
    version_id VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, catalog_id) REFERENCES catalogs (org_id, id),
    FOREIGN KEY(org_id, version_id) REFERENCES catalog_versions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, catalog_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_catalog_activations_org_id ON catalog_activations (org_id);

CREATE TABLE catalog_changes (
    catalog_id VARCHAR(36) NOT NULL,
    from_version_id VARCHAR(36) NOT NULL,
    to_version_id VARCHAR(36) NOT NULL,
    status VARCHAR(30) NOT NULL,
    links JSON NOT NULL,
    report JSON NOT NULL,
    error TEXT,
    created_by VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(org_id, catalog_id) REFERENCES catalogs (org_id, id),
    FOREIGN KEY(org_id, from_version_id) REFERENCES catalog_versions (org_id, id),
    FOREIGN KEY(org_id, to_version_id) REFERENCES catalog_versions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, from_version_id, to_version_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_catalog_changes_org_id ON catalog_changes (org_id);

CREATE TABLE batch_releases (
    batch_id VARCHAR(36) NOT NULL,
    baseline_run_id VARCHAR(36) NOT NULL,
    base_release_id VARCHAR(36),
    number INTEGER NOT NULL,
    status VARCHAR(30) NOT NULL,
    config JSON NOT NULL,
    version INTEGER NOT NULL,
    result_version INTEGER,
    manifest JSON NOT NULL,
    snapshot_hash VARCHAR(64),
    validation JSON NOT NULL,
    invalidated BOOLEAN NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    published_at VARCHAR(40),
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(created_by) REFERENCES users (id),
    FOREIGN KEY(org_id, base_release_id) REFERENCES batch_releases (org_id, id),
    FOREIGN KEY(org_id, baseline_run_id) REFERENCES match_runs (org_id, id),
    FOREIGN KEY(org_id, batch_id) REFERENCES batches (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, batch_id, number),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_batch_releases_org_id ON batch_releases (org_id);

CREATE UNIQUE INDEX uq_current_batch_release ON batch_releases (org_id, batch_id) WHERE status = 'PUBLISHED';

CREATE TABLE product_identities (
    product_id VARCHAR(36) NOT NULL,
    identity_id VARCHAR(36) NOT NULL,
    version_id VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, identity_id) REFERENCES catalog_identities (org_id, id),
    FOREIGN KEY(org_id, product_id) REFERENCES catalog_products (org_id, id),
    FOREIGN KEY(org_id, version_id) REFERENCES catalog_versions (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, identity_id, version_id),
    UNIQUE (org_id, product_id)
);

CREATE INDEX ix_product_identities_org_id ON product_identities (org_id);

CREATE TABLE source_identities (
    batch_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    stable_key VARCHAR(200) NOT NULL,
    reason TEXT NOT NULL,
    actor_id VARCHAR(36) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, batch_id) REFERENCES batches (org_id, id),
    FOREIGN KEY(org_id, source_id) REFERENCES source_records (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, source_id)
);

CREATE INDEX ix_source_identities_org_id ON source_identities (org_id);

CREATE TABLE annotation_tasks (
    dataset_id VARCHAR(36) NOT NULL,
    item_id VARCHAR(36) NOT NULL,
    product_id VARCHAR(36) NOT NULL,
    entity_key VARCHAR(200) NOT NULL,
    partition VARCHAR(20) NOT NULL,
    snapshot JSON NOT NULL,
    status VARCHAR(30) NOT NULL,
    label VARCHAR(30),
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, dataset_id) REFERENCES dataset_versions (org_id, id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id, product_id) REFERENCES catalog_products (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, dataset_id, item_id, product_id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_annotation_tasks_org_id ON annotation_tasks (org_id);

CREATE TABLE coordination_events (
    item_id VARCHAR(36) NOT NULL,
    actor_id VARCHAR(36) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    detail JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_coordination_events_org_id ON coordination_events (org_id);

CREATE TABLE deliveries (
    release_id VARCHAR(36) NOT NULL,
    integration_id VARCHAR(36) NOT NULL,
    event_id VARCHAR(36) NOT NULL,
    kind VARCHAR(40) NOT NULL,
    status VARCHAR(30) NOT NULL,
    attempts INTEGER NOT NULL,
    next_retry FLOAT NOT NULL,
    lease_until FLOAT NOT NULL,
    fence_token INTEGER NOT NULL,
    last_error TEXT,
    receipt JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, integration_id) REFERENCES integrations (org_id, id),
    FOREIGN KEY(org_id, release_id) REFERENCES batch_releases (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, integration_id, event_id)
);

CREATE INDEX ix_deliveries_org_id ON deliveries (org_id);

CREATE TABLE impact_tasks (
    change_id VARCHAR(36) NOT NULL,
    product_id VARCHAR(36) NOT NULL,
    item_id VARCHAR(36),
    release_id VARCHAR(36),
    resource_key VARCHAR(100) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    status VARCHAR(30) NOT NULL,
    resolution JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, change_id) REFERENCES catalog_changes (org_id, id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id, product_id) REFERENCES catalog_products (org_id, id),
    FOREIGN KEY(org_id, release_id) REFERENCES batch_releases (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, change_id, resource_key),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_impact_tasks_org_id ON impact_tasks (org_id);

CREATE TABLE release_artifacts (
    release_id VARCHAR(36) NOT NULL,
    kind VARCHAR(30) NOT NULL,
    status VARCHAR(30) NOT NULL,
    count INTEGER NOT NULL,
    object_key TEXT,
    file_hash VARCHAR(64),
    error TEXT,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, release_id) REFERENCES batch_releases (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, release_id, kind)
);

CREATE INDEX ix_release_artifacts_org_id ON release_artifacts (org_id);

CREATE TABLE release_events (
    release_id VARCHAR(36) NOT NULL,
    kind VARCHAR(40) NOT NULL,
    actor_id VARCHAR(36),
    detail JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, release_id) REFERENCES batch_releases (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_release_events_org_id ON release_events (org_id);

CREATE TABLE review_assignments (
    item_id VARCHAR(36) NOT NULL,
    assignee_id VARCHAR(36),
    assigned_by VARCHAR(36) NOT NULL,
    reason TEXT NOT NULL,
    updated_at VARCHAR(40) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(assigned_by) REFERENCES users (id),
    FOREIGN KEY(assignee_id) REFERENCES users (id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, item_id)
);

CREATE INDEX ix_review_assignments_org_id ON review_assignments (org_id);

CREATE TABLE review_claims (
    item_id VARCHAR(36) NOT NULL,
    holder_id VARCHAR(36),
    token_hash VARCHAR(64),
    lease_until FLOAT NOT NULL,
    status VARCHAR(30) NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(holder_id) REFERENCES users (id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, item_id)
);

CREATE INDEX ix_review_claims_org_id ON review_claims (org_id);

CREATE TABLE annotation_decisions (
    task_id VARCHAR(36) NOT NULL,
    actor_id VARCHAR(36) NOT NULL,
    kind VARCHAR(20) NOT NULL,
    label VARCHAR(30) NOT NULL,
    reason TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(actor_id) REFERENCES users (id),
    FOREIGN KEY(org_id, task_id) REFERENCES annotation_tasks (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, task_id, actor_id)
);

CREATE INDEX ix_annotation_decisions_org_id ON annotation_decisions (org_id);

CREATE TABLE delivery_attempts (
    delivery_id VARCHAR(36) NOT NULL,
    number INTEGER NOT NULL,
    status_code INTEGER,
    error TEXT,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, delivery_id) REFERENCES deliveries (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, delivery_id, number),
    UNIQUE (org_id, id)
);

CREATE INDEX ix_delivery_attempts_org_id ON delivery_attempts (org_id);

CREATE TABLE release_rows (
    release_id VARCHAR(36) NOT NULL,
    stable_key VARCHAR(200) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    item_id VARCHAR(36),
    decision_id VARCHAR(36),
    product_id VARCHAR(36),
    status VARCHAR(30) NOT NULL,
    data JSON NOT NULL,
    org_id VARCHAR(36) NOT NULL,
    id VARCHAR(36) NOT NULL,
    created_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(org_id, decision_id) REFERENCES review_events (org_id, id),
    FOREIGN KEY(org_id, item_id) REFERENCES match_items (org_id, id),
    FOREIGN KEY(org_id, product_id) REFERENCES catalog_products (org_id, id),
    FOREIGN KEY(org_id, release_id) REFERENCES batch_releases (org_id, id),
    FOREIGN KEY(org_id, source_id) REFERENCES source_records (org_id, id),
    FOREIGN KEY(org_id) REFERENCES organizations (id),
    UNIQUE (org_id, id),
    UNIQUE (org_id, release_id, stable_key)
);

CREATE INDEX ix_release_rows_item ON release_rows (org_id, item_id);

CREATE INDEX ix_release_rows_org_id ON release_rows (org_id);

CREATE INDEX ix_release_rows_product ON release_rows (org_id, product_id);

UPDATE alembic_version SET version_num='9b7798dcda5b' WHERE alembic_version.version_num = 'f484e73f04f0';

COMMIT;
