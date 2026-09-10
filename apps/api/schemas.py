from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    email: str = Field(max_length=200)
    password: str = Field(min_length=1, max_length=200)


class Named(Input):
    name: str = Field(min_length=1, max_length=200)


class ImportConfig(Input):
    file_id: str
    sheet: str
    header_row: int = Field(default=1, ge=1, le=100)
    mapping: dict[str, str]
    exclude_rows: list[int] = Field(default_factory=list, max_length=10000)


class BatchInput(ImportConfig):
    name: str = Field(min_length=1, max_length=200)
    supplier: str = Field(min_length=1, max_length=200)


class RunInput(Input):
    revision_id: str
    catalog_version_id: str
    policy_id: str | None = None


class VersionInput(Input):
    expected_version: int = Field(ge=0)


class Reason(Input):
    reason: str = Field(min_length=1, max_length=2000)


class Decision(VersionInput):
    claim_token: str | None = Field(default=None, max_length=200)
    action: Literal["confirm", "unmatched", "needs_info", "revoke"]
    product_id: str | None = None
    reason: str = Field(default="", max_length=2000)


class BulkEntry(Decision):
    item_id: str


class Bulk(Input):
    items: list[BulkEntry] = Field(min_length=1, max_length=100)


class ExportInput(Input):
    kind: Literal["confirmed", "all"] = "confirmed"
    format: Literal["csv", "xlsx"] = "xlsx"
    include_raw: bool = False
    status: str | None = None
    suggestion: str | None = None


class MemberInput(Input):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=10, max_length=200)
    roles: list[Literal["operator", "reviewer", "admin", "viewer", "publisher", "supervisor", "integration_manager", "annotator", "adjudicator"]] = Field(min_length=1)


class MemberUpdate(Input):
    roles: list[Literal["operator", "reviewer", "admin", "viewer", "publisher", "supervisor", "integration_manager", "annotator", "adjudicator"]] = Field(min_length=1)
    active: bool


class SettingsInput(Input):
    dual_review: bool
    default_policy_id: str


class PolicyInput(Named):
    engine: Literal["rules", "lightgbm"] = "rules"
    high_threshold: float = Field(default=0.90, ge=0, le=1)
    margin: float = Field(default=0.08, ge=0, le=1)
    model_id: str | None = None
    evaluation_id: str | None = None


class TemplateInput(Named):
    supplier: str = Field(min_length=1, max_length=200)
    headers: list[str] = Field(min_length=1, max_length=100)
    mapping: dict[str, str]


class CorrectionInput(VersionInput):
    fields: dict[str, str]
    evidence: str = Field(min_length=2, max_length=2000)


class CorrectionSubmit(Input):
    draft_ids: list[str] = Field(min_length=1, max_length=10000)


class ShadowInput(Input):
    evaluation_id: str


class UsageInput(Input):
    run_id: str
    item_id: str | None = None
    event: Literal['select', 'review', 'search', 'correction', 'interruption']
    duration_ms: int | None = Field(default=None, ge=0, le=86400000)
    trial_id: str | None = Field(default=None, max_length=100)
