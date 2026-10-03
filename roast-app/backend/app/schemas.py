"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SampleIn(BaseModel):
    t_s: float
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    sampled_at: datetime | None = None


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
# roast plan versions
# ---------------------------------------------------------------------------

class ShapePointIn(BaseModel):
    offset_frac: float = Field(gt=0, lt=1)
    target_temp_c: float
    tolerance_c: float = Field(ge=0)


class SegmentIn(BaseModel):
    id: str
    from_anchor: str | None = None
    to_anchor: str | None = None
    target_duration_s: float = Field(gt=0)
    duration_tolerance_s: float = Field(ge=0)
    end_temp_c: float | None = None
    end_temp_tolerance_c: float | None = None
    start_temp_c: float | None = None
    shape: list[ShapePointIn] = []


class PlanDefinitionIn(BaseModel):
    segments: list[SegmentIn] = Field(min_length=1)
    anchor_schedule: dict[str, float] = {}


class PlanCreateIn(BaseModel):
    name: str = Field(min_length=1, max=120)
    description: str = ""
    created_by: str = "roast-lead"
    definition: PlanDefinitionIn


class PlanVersionDraftIn(BaseModel):
    """Mint a NEW draft version from a (confirmed/retired) base version."""
    based_on_version_id: int
    note: str = ""
    created_by: str = "roast-lead"
    definition: PlanDefinitionIn | None = None  # defaults to the base content


class PlanVersionEditIn(BaseModel):
    """Edit the single open draft.  ``expected_revision`` makes two editors
    editing the same draft collide explicitly (HTTP 409)."""
    definition: PlanDefinitionIn
    expected_revision: int = Field(ge=1)
    note: str | None = None
    created_by: str = "roast-lead"


class PlanStateIn(BaseModel):
    by: str = "roast-lead"
    note: str = ""


class PlanVersionOut(BaseModel):
    id: int
    plan_id: int
    version_no: int
    status: str
    definition: dict[str, Any]
    content_hash: str
    based_on_version_id: int | None = None
    revision: int
    note: str
    created_by: str
    confirmed_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime


class PlanOut(BaseModel):
    id: int
    name: str
    description: str
    created_by: str
    created_at: datetime
    current_version: PlanVersionOut | None = None
    versions: list[PlanVersionOut] = []


class BindingCreateIn(BaseModel):
    plan_version_id: int
    bound_by: str = "operator"
    note: str = ""
    # Optional: reject if the batch is already on a different current binding
    # unless the caller acknowledges this rebinds (old binding is preserved).
    expected_binding_id: int | None = None


class BindingOut(BaseModel):
    id: int
    batch_id: int
    plan_version_id: int
    bound_by: str
    note: str
    superseded: bool
    created_at: datetime
    plan_version: PlanVersionOut | None = None


class AssessmentOut(BaseModel):
    id: int
    binding_id: int
    batch_id: int
    plan_version_id: int
    review_state: str
    verdict: str
    needs_review_reason: str
    result: dict[str, Any]
    content_hash: str
    max_gap_fill_s: float
    assessed_by: str
    superseded_by_id: int | None = None
    created_at: datetime
