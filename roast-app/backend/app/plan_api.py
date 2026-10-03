"""Service + router for immutable roast-plan versions, bindings, assessments.

Lifecycle (per plan chain, strictly linear)::

    draft v1 ──confirm──▶ confirmed v1 ──new draft (based on v1)──▶ draft v2
                                  └──────── confirming v2 retires v1 ┘

* Confirmed/retired rows are immutable content; no endpoint rewrites them.
* Every "edit after binding" mints a NEW version row; batches and exports keep
  pointing at the version they were judged with.
* Two editors basing a change on the same old version collide: the second
  ``POST .../versions`` gets HTTP 409 (concurrent draft edits also collide via
  ``expected_revision``).
* Assessments are append-only conclusions: an anchor correction marks the
  current one ``needs_review`` (basis retained); recomputing supersedes it.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import plans as plan_lib
from .analysis import RoRConfig, build_series
from .config import MAX_GAP_FILL_S
from .models import (
    ASSESSMENT_CURRENT,
    ASSESSMENT_NEEDS_REVIEW,
    ASSESSMENT_SUPERSEDED,
    Batch,
    BatchPlanBinding,
    PlanAssessment,
    RoastPlan,
    RoastPlanVersion,
    engine,
)
from .schemas import (
    BindingCreateIn,
    PlanCreateIn,
    PlanStateIn,
    PlanVersionDraftIn,
    PlanVersionEditIn,
)

router = APIRouter(prefix="/api", tags=["roast-plans"])

# Events that, when corrected, can move an anchor and invalidate a judgement.
ANCHOR_EVENT_TYPES = set(plan_lib.PLAN_ANCHORS)


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------

def _definition(row: RoastPlanVersion) -> dict[str, Any]:
    return json.loads(row.definition_json)


def version_out(row: RoastPlanVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "plan_id": row.plan_id,
        "version_no": row.version_no,
        "status": row.status,
        "definition": _definition(row),
        "content_hash": row.content_hash,
        "based_on_version_id": row.based_on_version_id,
        "revision": row.revision,
        "note": row.note,
        "created_by": row.created_by,
        "confirmed_at": row.confirmed_at,
        "retired_at": row.retired_at,
        "created_at": row.created_at,
    }


def plan_out(
    row: RoastPlan, *, versions: list[RoastPlanVersion] | None = None
) -> dict[str, Any]:
    versions = versions if versions is not None else list(row.versions)
    current = next((v for v in versions if v.status == "confirmed"), None)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "current_version": version_out(current) if current else None,
        "versions": [version_out(v) for v in versions],
    }


def assessment_out(row: PlanAssessment) -> dict[str, Any]:
    return {
        "id": row.id,
        "binding_id": row.binding_id,
        "batch_id": row.batch_id,
        "plan_version_id": row.plan_version_id,
        "review_state": row.review_state,
        "verdict": row.verdict,
        "needs_review_reason": row.needs_review_reason,
        "result": json.loads(row.result_json),
        "content_hash": row.content_hash,
        "max_gap_fill_s": row.max_gap_fill_s,
        "assessed_by": row.assessed_by,
        "superseded_by_id": row.superseded_by_id,
        "created_at": row.created_at,
    }


def binding_out(s: Session, row: BatchPlanBinding) -> dict[str, Any]:
    v = s.get(RoastPlanVersion, row.plan_version_id)
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "plan_version_id": row.plan_version_id,
        "bound_by": row.bound_by,
        "note": row.note,
        "superseded": row.superseded,
        "created_at": row.created_at,
        "plan_version": version_out(v) if v else None,
    }


# ---------------------------------------------------------------------------
# lookup helpers
# ---------------------------------------------------------------------------

def _get_plan(s: Session, plan_id: int) -> RoastPlan:
    p = s.get(RoastPlan, plan_id)
    if p is None:
        raise HTTPException(404, f"plan {plan_id} not found")
    return p


def _get_version(s: Session, version_id: int) -> RoastPlanVersion:
    v = s.get(RoastPlanVersion, version_id)
    if v is None:
        raise HTTPException(404, f"plan version {version_id} not found")
    return v


def current_binding(s: Session, batch_id: int) -> BatchPlanBinding | None:
    """Latest binding of a batch (the non-superseded one), if any."""
    return s.scalar(
        select(BatchPlanBinding)
        .where(
            BatchPlanBinding.batch_id == batch_id,
            BatchPlanBinding.superseded.is_(False),
        )
        .order_by(BatchPlanBinding.id.desc())
    )


def current_assessment(s: Session, batch_id: int) -> PlanAssessment | None:
    """Latest live judgement (current or flagged needs_review) of the batch."""
    return s.scalar(
        select(PlanAssessment)
        .where(
            PlanAssessment.batch_id == batch_id,
            PlanAssessment.review_state.in_(
                [ASSESSMENT_CURRENT, ASSESSMENT_NEEDS_REVIEW]
            ),
        )
        .order_by(PlanAssessment.id.desc())
    )


def latest_assessment_for_binding(
    s: Session, binding_id: int
) -> PlanAssessment | None:
    return s.scalar(
        select(PlanAssessment)
        .where(PlanAssessment.binding_id == binding_id)
        .order_by(PlanAssessment.id.desc())
    )


# ---------------------------------------------------------------------------
# plan/version lifecycle
# ---------------------------------------------------------------------------

def _validate(definition: dict) -> tuple[str, str]:
    """Return (canonical_json_text, sha256); 422 on a structurally bad doc."""
    try:
        text = plan_lib.canonical_json(definition)
    except plan_lib.PlanDefinitionError as exc:
        raise HTTPException(422, f"invalid plan definition: {exc}")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _conflict(detail: dict[str, Any]) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


@router.post("/plans", status_code=201)
def create_plan(parsed: PlanCreateIn) -> dict[str, Any]:
    """Create a plan plus its initial draft (version 1)."""
    defn_text, h = _validate(parsed.definition.model_dump(exclude_none=True))
    with Session(engine) as s:
        if s.scalar(select(RoastPlan).where(RoastPlan.name == parsed.name)):
            raise HTTPException(409, {"error": "name_taken", "name": parsed.name})
        plan = RoastPlan(
            name=parsed.name,
            description=parsed.description,
            created_by=parsed.created_by,
        )
        s.add(plan)
        s.flush()
        v = RoastPlanVersion(
            plan_id=plan.id,
            version_no=1,
            status="draft",
            definition_json=defn_text,
            content_hash=h,
            based_on_version_id=None,
            note="initial draft",
            created_by=parsed.created_by,
            revision=1,
        )
        s.add(v)
        s.commit()
        s.refresh(v)
        s.refresh(plan)
        return plan_out(plan, versions=[v])


@router.post("/seed-demo-plan", status_code=201)
def seed_demo_plan() -> dict[str, Any]:
    """Create+confirm the demo plan and bind both demo batches, computing
    their initial assessments.  Idempotent by plan name."""
    from .synth import demo_plan_definition

    defn = demo_plan_definition()
    defn_text, h = _validate(defn)
    with Session(engine) as s:
        plan = s.scalar(select(RoastPlan).where(RoastPlan.name == "DEMO-PLAN-2026-09"))
        if plan is None:
            plan = RoastPlan(
                name="DEMO-PLAN-2026-09",
                description="演示方案：按 charge/回温点/一爆/drop 锚点定位，含容差带",
                created_by="seed-recipe",
            )
            s.add(plan)
            s.flush()
            v = RoastPlanVersion(
                plan_id=plan.id,
                version_no=1,
                status="confirmed",
                definition_json=defn_text,
                content_hash=h,
                note="seeded confirmed demo plan",
                created_by="seed-recipe",
                revision=1,
                confirmed_at=datetime.utcnow(),
            )
            s.add(v)
            s.flush()
        else:
            v = s.scalar(
                select(RoastPlanVersion).where(
                    RoastPlanVersion.plan_id == plan.id,
                    RoastPlanVersion.status == "confirmed",
                )
            )
            if v is None:
                raise HTTPException(
                    409,
                    {"error": "demo_plan_has_no_confirmed_version", "plan_id": plan.id},
                )

        demo_batches = list(
            s.scalars(select(Batch).where(Batch.name.like("SYN-2026-0920-%")))
        )
        bound = []
        for b in demo_batches:
            if current_binding(s, b.id) is not None:
                continue
            row = BatchPlanBinding(
                batch_id=b.id,
                plan_version_id=v.id,
                bound_by="seed-recipe",
                note="demo binding",
            )
            s.add(row)
            s.flush()
            assess_binding(s, row, assessed_by="seed-recipe")
            bound.append(b.id)
        s.commit()
        return {
            "plan": plan_out(plan, versions=list(plan.versions)),
            "bound_batch_ids": bound,
        }


@router.get("/plans")
def list_plans() -> list[dict[str, Any]]:
    with Session(engine) as s:
        plans = list(s.scalars(select(RoastPlan).order_by(RoastPlan.id)))
        return [plan_out(p) for p in plans]


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        return plan_out(_get_plan(s, plan_id))


@router.get("/plans/{plan_id}/versions/{version_id}")
def get_version(plan_id: int, version_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        v = _get_version(s, version_id)
        if v.plan_id != plan_id:
            raise HTTPException(404, "version does not belong to this plan")
        return version_out(v)


@router.post("/plans/{plan_id}/versions", status_code=201)
def new_draft_version(plan_id: int, parsed: PlanVersionDraftIn) -> dict[str, Any]:
    """Mint a NEW draft from a confirmed/retired base version.

    Optimistic concurrency: the base must be the newest version in the chain.
    If another editor already created a descendant (e.g. draft v3 on top of
    v2), this second submit returns 409 and never overwrites their version.
    """
    with Session(engine) as s:
        plan = _get_plan(s, plan_id)
        base = _get_version(s, parsed.based_on_version_id)
        if base.plan_id != plan.id:
            raise HTTPException(404, "base version does not belong to this plan")
        if base.status == "draft":
            raise HTTPException(
                422,
                {
                    "error": "base_is_draft",
                    "message": "基础版本仍是草稿：请直接编辑该草稿，而不是再派生草稿",
                    "draft_version_id": base.id,
                },
            )

        versions = list(s.scalars(
            select(RoastPlanVersion)
            .where(RoastPlanVersion.plan_id == plan.id)
        ))
        newest = max(versions, key=lambda v: v.version_no)
        if newest.id != base.id:
            raise _conflict({
                "error": "version_conflict",
                "message": (
                    f"基础版本 v{base.version_no} 已不是该方案的最新版本"
                    f"（已有 v{newest.version_no}）。请基于最新版本重新提交，"
                    "本次改动未写入，不会覆盖先到的版本。"
                ),
                "base_version_id": base.id,
                "latest_version_id": newest.id,
                "latest_version_no": newest.version_no,
            })

        open_draft = next((v for v in versions if v.status == "draft"), None)
        if open_draft is not None:
            raise _conflict({
                "error": "open_draft_exists",
                "message": "该方案已有未处理草稿；同一方案只能有一条开放草稿链。",
                "draft_version_id": open_draft.id,
            })

        if parsed.definition is not None:
            defn_text, h = _validate(parsed.definition.model_dump(exclude_none=True))
        else:
            defn_text, h = base.definition_json, base.content_hash
        v = RoastPlanVersion(
            plan_id=plan.id,
            version_no=base.version_no + 1,
            status="draft",
            definition_json=defn_text,
            content_hash=h,
            based_on_version_id=base.id,
            note=parsed.note or f"draft derived from v{base.version_no}",
            created_by=parsed.created_by,
            revision=1,
        )
        s.add(v)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            # Two editors created descendants of the same base concurrently;
            # the unique (plan_id, version_no) constraint rejected the loser.
            raise _conflict({
                "error": "version_conflict",
                "message": (
                    "同一基础版本已被另一端抢先派生出新版本；本次改动未写入，"
                    "请基于最新版本重新提交。"
                ),
                "base_version_id": base.id,
            })
        s.refresh(v)
        return version_out(v)


@router.patch("/plans/{plan_id}/versions/{version_id}")
def edit_draft(plan_id: int, version_id: int, parsed: PlanVersionEditIn) -> dict[str, Any]:
    """Edit the single open draft.  Never touches confirmed/retired rows."""
    with Session(engine) as s:
        v = _get_version(s, version_id)
        if v.plan_id != plan_id:
            raise HTTPException(404, "version does not belong to this plan")
        if v.status != "draft":
            raise HTTPException(
                422,
                {
                    "error": "version_immutable",
                    "message": (
                        f"v{v.version_no} 已{v.status}，内容不可变；"
                        "调整目标只能基于它创建新版本，不能改写历史版本。"
                    ),
                    "version_id": v.id,
                    "status": v.status,
                },
            )
        defn_text, h = _validate(parsed.definition.model_dump(exclude_none=True))
        # Atomic optimistic update: the WHERE clause only matches when nobody
        # else bumped the revision first, so two concurrent PATCHes cannot
        # silently overwrite one another (works on SQLite, where row locks do
        # not exist, as well as PostgreSQL).
        new_note = parsed.note if parsed.note is not None else v.note
        result = s.execute(
            update(RoastPlanVersion)
            .where(
                RoastPlanVersion.id == v.id,
                RoastPlanVersion.status == "draft",
                RoastPlanVersion.revision == parsed.expected_revision,
            )
            .values(
                definition_json=defn_text,
                content_hash=h,
                revision=parsed.expected_revision + 1,
                note=new_note,
                created_by=parsed.created_by,
            )
        )
        if result.rowcount != 1:
            s.rollback()
            current = _get_version(s, version_id)
            raise _conflict({
                "error": "draft_revision_conflict",
                "message": (
                    f"草稿 v{current.version_no} 已被另一端更新（当前 revision="
                    f"{current.revision}，提交基于 revision={parsed.expected_revision}）；"
                    "请刷新后在最新内容上合并改动。"
                ),
                "current_revision": current.revision,
                "submitted_revision": parsed.expected_revision,
            })
        s.commit()
        s.refresh(v)
        return version_out(v)


@router.post("/plans/{plan_id}/versions/{version_id}/confirm")
def confirm_version(
    plan_id: int, version_id: int, parsed: PlanStateIn | None = None
) -> dict[str, Any]:
    """draft -> confirmed.  Confirming retires the previously confirmed version
    of the same plan (its content and all bindings to it are untouched)."""
    parsed = parsed or PlanStateIn()
    with Session(engine) as s:
        plan = _get_plan(s, plan_id)
        v = _get_version(s, version_id)
        if v.plan_id != plan.id:
            raise HTTPException(404, "version does not belong to this plan")
        if v.status != "draft":
            raise HTTPException(
                422,
                {
                    "error": "not_a_draft",
                    "message": f"v{v.version_no} 状态为 {v.status}，只有草稿可确认",
                },
            )
        previous = list(s.scalars(
            select(RoastPlanVersion).where(
                RoastPlanVersion.plan_id == plan.id,
                RoastPlanVersion.status == "confirmed",
            )
        ))
        now = datetime.utcnow()
        for old in previous:
            old.status = "retired"
            old.retired_at = now
            old.note = (old.note + f" | retired when v{v.version_no} confirmed").strip(" |")
        v.status = "confirmed"
        v.confirmed_at = now
        v.note = (v.note + f" | confirmed by {parsed.by}").strip(" |")
        s.commit()
        s.refresh(v)
        return version_out(v)


@router.post("/plans/{plan_id}/versions/{version_id}/retire")
def retire_version(
    plan_id: int, version_id: int, parsed: PlanStateIn | None = None
) -> dict[str, Any]:
    """confirmed -> retired (explicit).  Existing bindings/assessments remain."""
    parsed = parsed or PlanStateIn()
    with Session(engine) as s:
        plan = _get_plan(s, plan_id)
        v = _get_version(s, version_id)
        if v.plan_id != plan.id:
            raise HTTPException(404, "version does not belong to this plan")
        if v.status != "confirmed":
            raise HTTPException(
                422,
                {
                    "error": "not_confirmed",
                    "message": f"v{v.version_no} 状态为 {v.status}，只有已确认版本可退役",
                },
            )
        v.status = "retired"
        v.retired_at = datetime.utcnow()
        v.note = (v.note + f" | retired by {parsed.by}: {parsed.note}").strip(" |")
        s.commit()
        s.refresh(v)
        return version_out(v)


# ---------------------------------------------------------------------------
# bindings
# ---------------------------------------------------------------------------

@router.put("/batches/{batch_id}/plan-binding")
def bind_batch(batch_id: int, parsed: BindingCreateIn) -> dict[str, Any]:
    """Bind a batch to a CONFIRMED version (immutable link).

    Rebinding to a newer confirmed version inserts a new row and supersedes the
    previous binding; the old row (and its assessment history) is retained.
    """
    with Session(engine) as s:
        b = s.get(Batch, batch_id)
        if b is None:
            raise HTTPException(404, f"batch {batch_id} not found")
        v = _get_version(s, parsed.plan_version_id)
        if v.status != "confirmed":
            raise HTTPException(
                422,
                {
                    "error": "version_not_confirmed",
                    "message": (
                        f"v{v.version_no} 状态为 {v.status}：批次只能绑定已确认版本"
                    ),
                    "version_id": v.id,
                },
            )
        existing = current_binding(s, batch_id)
        if existing is not None:
            if parsed.expected_binding_id not in (None, existing.id):
                raise _conflict({
                    "error": "binding_conflict",
                    "message": "批次当前绑定已被另一端改变，请刷新后确认是否重新绑定。",
                    "current_binding_id": existing.id,
                })
            if existing.plan_version_id == v.id:
                return binding_out(s, existing)  # idempotent
            existing.superseded = True
            # The previous binding's live judgement belongs to the old version:
            # retire it (row + basis retained in history) rather than letting a
            # stale verdict masquerade as current for the new binding.
            old_live = latest_assessment_for_binding(s, existing.id)
            if old_live is not None and old_live.review_state != ASSESSMENT_SUPERSEDED:
                old_live.review_state = ASSESSMENT_SUPERSEDED
                old_live.needs_review_reason = (
                    (old_live.needs_review_reason + " " if old_live.needs_review_reason else "")
                    + "批次已改绑新版本，此为旧版本的历史判断。"
                )
        row = BatchPlanBinding(
            batch_id=batch_id,
            plan_version_id=v.id,
            bound_by=parsed.bound_by,
            note=parsed.note,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return binding_out(s, row)


@router.get("/batches/{batch_id}/plan-binding")
def get_binding(batch_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        if s.get(Batch, batch_id) is None:
            raise HTTPException(404, f"batch {batch_id} not found")
        row = current_binding(s, batch_id)
        if row is None:
            raise HTTPException(404, {"error": "no_binding", "batch_id": batch_id})
        return binding_out(s, row)


@router.get("/batches/{batch_id}/plan-binding/history")
def binding_history(batch_id: int) -> list[dict[str, Any]]:
    with Session(engine) as s:
        if s.get(Batch, batch_id) is None:
            raise HTTPException(404, f"batch {batch_id} not found")
        rows = list(s.scalars(
            select(BatchPlanBinding)
            .where(BatchPlanBinding.batch_id == batch_id)
            .order_by(BatchPlanBinding.id)
        ))
        return [binding_out(s, r) for r in rows]


# ---------------------------------------------------------------------------
# assessments (persistent deviation conclusions)
# ---------------------------------------------------------------------------

def _samples_dicts(b: Batch) -> list[dict]:
    return [
        {"t_s": sp.t_s, "bean_temp_c": sp.bean_temp_c, "env_temp_c": sp.env_temp_c}
        for sp in b.samples
    ]


def _events_dicts(b: Batch) -> list[dict]:
    return [
        {
            "id": e.id,
            "batch_id": e.batch_id,
            "event_type": e.event_type,
            "t_s": e.t_s,
            "label": e.label,
            "source": e.source,
            "created_by": e.created_by,
            "value_num": e.value_num,
            "note": e.note,
            "superseded": e.superseded,
            "superseded_by_id": e.superseded_by_id,
            "created_at": e.created_at.isoformat(),
        }
        for e in b.events
    ]


def compute_result(
    s: Session,
    b: Batch,
    version: RoastPlanVersion,
    *,
    max_gap_fill_s: float = MAX_GAP_FILL_S,
) -> dict[str, Any]:
    """Pure recomputation from stored raw samples + current events."""
    series = build_series(
        _samples_dicts(b),
        ror_cfg=RoRConfig(),
        max_gap_fill_s=max_gap_fill_s,
    )
    result = plan_lib.evaluate_plan(
        _definition(version),
        plan_lib.attach_guide(series["raw_points"], series["guide_bean_temp"]),
        _events_dicts(b),
        max_gap_fill_s=max_gap_fill_s,
        missing_segments=series["missing_segments"],
    )
    result["basis"] = {
        "plan_version_id": version.id,
        "plan_id": version.plan_id,
        "plan_version_no": version.version_no,
        "plan_status_at_assessment": version.status,
        "plan_content_hash": version.content_hash,
        "max_gap_fill_s": float(max_gap_fill_s),
        "anchor_events": [
            {
                "event_type": e.event_type,
                "t_s": e.t_s,
                "source": e.source,
                "id": e.id,
            }
            for e in b.events
            if not e.superseded and e.event_type in ANCHOR_EVENT_TYPES
        ],
    }
    return result


def _result_hash(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assess_binding(
    s: Session,
    binding: BatchPlanBinding,
    *,
    assessed_by: str = "system",
) -> PlanAssessment:
    """Insert a fresh assessment and supersede the binding's previous live one.

    Must run inside the caller's transaction (no commit here)."""
    b = s.get(Batch, binding.batch_id)
    version = _get_version(s, binding.plan_version_id)
    result = compute_result(s, b, version)
    h = _result_hash(result)
    prev = latest_assessment_for_binding(s, binding.id)
    row = PlanAssessment(
        binding_id=binding.id,
        batch_id=binding.batch_id,
        plan_version_id=binding.plan_version_id,
        review_state=ASSESSMENT_CURRENT,
        verdict=result["verdict"],
        result_json=json.dumps(result, ensure_ascii=False),
        content_hash=h,
        max_gap_fill_s=MAX_GAP_FILL_S,
        assessed_by=assessed_by,
    )
    s.add(row)
    s.flush()
    if prev is not None and prev.review_state != ASSESSMENT_SUPERSEDED:
        prev.review_state = ASSESSMENT_SUPERSEDED
        prev.superseded_by_id = row.id
    return row


@router.post("/batches/{batch_id}/plan-assessments", status_code=201)
def run_assessment(batch_id: int, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    """(Re)compute the deviation conclusion for the batch's CURRENT binding.

    If the current judgement is ``needs_review`` after an anchor correction,
    recomputing supersedes it and stores the new conclusion; the stale row and
    its basis remain available under .../history.
    """
    assessed_by = (parsed or {}).get("assessed_by", "operator")
    with Session(engine) as s:
        b = s.get(Batch, batch_id)
        if b is None:
            raise HTTPException(404, f"batch {batch_id} not found")
        binding = current_binding(s, batch_id)
        if binding is None:
            raise HTTPException(
                422,
                {"error": "no_binding", "message": "批次尚未绑定已确认方案版本"},
            )
        row = assess_binding(s, binding, assessed_by=assessed_by)
        s.commit()
        s.refresh(row)
        return assessment_out(row)


@router.get("/batches/{batch_id}/plan-assessment")
def get_assessment(batch_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        if s.get(Batch, batch_id) is None:
            raise HTTPException(404, f"batch {batch_id} not found")
        row = current_assessment(s, batch_id)
        if row is None:
            raise HTTPException(404, {"error": "no_assessment", "batch_id": batch_id})
        return assessment_out(row)


@router.get("/batches/{batch_id}/plan-assessment/history")
def assessment_history(batch_id: int) -> list[dict[str, Any]]:
    with Session(engine) as s:
        if s.get(Batch, batch_id) is None:
            raise HTTPException(404, f"batch {batch_id} not found")
        rows = list(s.scalars(
            select(PlanAssessment)
            .where(PlanAssessment.batch_id == batch_id)
            .order_by(PlanAssessment.id)
        ))
        return [assessment_out(r) for r in rows]


def mark_stale_for_anchor_event(
    s: Session, batch_id: int, event_type: str, new_t_s: float
) -> list[PlanAssessment]:
    """After an anchor correction: flag the batch's live judgement for review.

    Does NOT delete or overwrite the old conclusion — its verdict and full
    basis stay queryable.  Returns the flagged rows (caller commits).
    """
    if event_type not in ANCHOR_EVENT_TYPES:
        return []
    row = current_assessment(s, batch_id)
    if row is None or row.review_state != ASSESSMENT_CURRENT:
        # Nothing live to flag (absent, already needs_review, or superseded):
        # don't claim THIS correction caused a new flag.
        return []
    row.review_state = ASSESSMENT_NEEDS_REVIEW
    row.needs_review_reason = (
        f"锚点事件 {event_type} 被人工修正为 t={new_t_s:g}s；"
        f"依赖该锚点的方案判断需要重新审阅。以下结论为修正前的历史判断，其依据保留可查。"
    )
    return [row]
