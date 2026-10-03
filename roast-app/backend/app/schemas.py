"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SampleIn(BaseModel):
    t_s: float
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    sampled_at: datetime | None = None


class BatchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    roaster: str = "manual-entry"
    bean: str = ""
    charge_at: datetime | None = None
    charge_temp_c: float | None = None
    ambient_temp_c: float | None = 22.0
    target_drop_temp_c: float | None = None
    note: str = ""
    samples: list[SampleIn] = []
    events: list["EventIn"] = []


class EventIn(BaseModel):
    event_type: str
    t_s: float = Field(ge=0)
    label: str = ""
    source: str = "manual"
    created_by: str = "operator"
    value_num: float | None = None
    note: str = ""


class EventOut(EventIn):
    id: int
    batch_id: int
    superseded: bool
    superseded_by_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class BatchMeta(BaseModel):
    id: int
    name: str
    roaster: str
    bean: str
    charge_at: datetime
    charge_temp_c: float
    ambient_temp_c: float
    target_drop_temp_c: float | None = None
    note: str

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# roasting plans: immutable versions, bindings, stored evaluations
# ---------------------------------------------------------------------------

class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max=120)
    bean: str = ""
    note: str = ""
    created_by: str = "roast-lead"
    # Content for the first (draft) version.  Structural validation happens in
    # plan_eval.validate_plan_content so errors are operator-readable Chinese.
    content: dict[str, Any]
    change_note: str = "初稿"


class PlanVersionCreate(BaseModel):
    # The version_no the editor started from.  If another commit already moved
    # the plan HEAD, the server rejects this submit with 409 instead of
    # silently branching/overwriting.
    base_version_no: int = Field(ge=1)
    content: dict[str, Any]
    change_note: str = ""
    created_by: str = "roast-lead"


class PlanStatusUpdate(BaseModel):
    expected_status: Literal["draft", "confirmed", "retired"] | None = None
    note: str = ""


class PlanBindingCreate(BaseModel):
    plan_version_id: int
    created_by: str = "operator"


class ReReviewRequest(BaseModel):
    note: str = ""
    created_by: str = "operator"


class PlanVersionOut(BaseModel):
    id: int
    plan_id: int
    version_no: int
    status: str
    content: dict[str, Any]
    content_sha256: str
    change_note: str
    created_by: str
    base_version_no: int | None = None
    created_at: datetime
    confirmed_at: datetime | None = None
    retired_at: datetime | None = None

    class Config:
        from_attributes = True


class PlanOut(BaseModel):
    id: int
    name: str
    bean: str
    note: str
    created_by: str
    created_at: datetime
    versions: list[PlanVersionOut] = []

    class Config:
        from_attributes = True


class PlanEvaluationOut(BaseModel):
    id: int
    binding_id: int
    review_state: str
    result: dict[str, Any]
    content_sha256: str
    anchor_fingerprint: str
    note: str
    created_by: str
    created_at: datetime
    reviewed_at: datetime | None = None

    class Config:
        from_attributes = True


class PlanBindingOut(BaseModel):
    id: int
    batch_id: int
    plan_id: int
    plan_version_id: int
    superseded: bool
    superseded_by_id: int | None = None
    created_by: str
    created_at: datetime
    plan_version: PlanVersionOut | None = None
    evaluations: list[PlanEvaluationOut] = []

    class Config:
        from_attributes = True
