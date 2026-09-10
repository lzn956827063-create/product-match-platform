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

COMMIT;
