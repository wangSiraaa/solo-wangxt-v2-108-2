"""FastAPI application: batch curves, sourced events, comparison, export."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import plan_eval, synth
from .analysis import RoRConfig, build_series, current_events, phase_metrics
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .default_plan import DEFAULT_PLAN_CONTENT, DEFAULT_PLAN_NAME
from .models import (
    Batch,
    Event,
    PlanBinding,
    PlanEvaluation,
    RoastPlan,
    RoastPlanVersion,
    Sample,
    engine,
    init_db,
)
from .schemas import (
    BatchCreate,
    BatchMeta,
    EventIn,
    EventOut,
    PlanBindingCreate,
    PlanCreate,
    PlanStatusUpdate,
    PlanVersionCreate,
    ReReviewRequest,
)

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_batch(session: Session, batch_id: int) -> Batch:
    b = session.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return b


def _samples_as_dicts(batch: Batch) -> list[dict]:
    return [
        {
            "t_s": s.t_s,
            "bean_temp_c": s.bean_temp_c,
            "env_temp_c": s.env_temp_c,
        }
        for s in batch.samples
    ]


def _events_as_dicts(batch: Batch, *, include_history: bool) -> list[dict]:
    rows = []
    for e in batch.events:
        if not include_history and e.superseded:
            continue
        rows.append(
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
        )
    return rows


def _series_payload(
    batch: Batch,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
    session: Session | None = None,
) -> dict[str, Any]:
    series = build_series(
        _samples_as_dicts(batch),
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
    )
    events = _events_as_dicts(batch, include_history=include_history)
    payload: dict[str, Any] = {
        "batch": BatchMeta.model_validate(batch).model_dump(mode="json"),
        "series": series,
        "events": events,
        "metrics": phase_metrics(events),
        "params": {
            "ror_window_s": window_s,
            "ror_display_smooth_s": display_smooth_s,
            "max_gap_fill_s": max_gap_fill_s,
            "raw_is_immutable": True,
        },
    }
    payload["plan"] = _batch_plan_section(batch, session)
    return payload


# ---------------------------------------------------------------------------
# plan / binding / evaluation helpers
# ---------------------------------------------------------------------------

def _version_out(v: RoastPlanVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "plan_id": v.plan_id,
        "version_no": v.version_no,
        "status": v.status,
        "content": v.content,
        "content_sha256": v.content_sha256,
        "change_note": v.change_note,
        "created_by": v.created_by,
        "base_version_no": v.base_version_no,
        "created_at": v.created_at.isoformat(),
        "confirmed_at": v.confirmed_at.isoformat() if v.confirmed_at else None,
        "retired_at": v.retired_at.isoformat() if v.retired_at else None,
    }


def _plan_out(p: RoastPlan) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "bean": p.bean,
        "note": p.note,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat(),
        "versions": [_version_out(v) for v in p.versions],
    }


def _evaluation_out(r: PlanEvaluation) -> dict[str, Any]:
    return {
        "id": r.id,
        "binding_id": r.binding_id,
        "review_state": r.review_state,
        "result": r.result,
        "content_sha256": r.content_sha256,
        "anchor_fingerprint": r.anchor_fingerprint,
        "note": r.note,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat(),
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
    }


def _binding_out(b: PlanBinding) -> dict[str, Any]:
    return {
        "id": b.id,
        "batch_id": b.batch_id,
        "plan_id": b.plan_id,
        "plan_version_id": b.plan_version_id,
        "superseded": b.superseded,
        "superseded_by_id": b.superseded_by_id,
        "created_by": b.created_by,
        "created_at": b.created_at.isoformat(),
        "plan_version": _version_out(b.plan_version) if b.plan_version else None,
        "evaluations": [_evaluation_out(r) for r in b.evaluations],
    }


def _current_binding(session: Session, batch_id: int) -> PlanBinding | None:
    return session.scalar(
        select(PlanBinding)
        .where(PlanBinding.batch_id == batch_id, PlanBinding.superseded.is_(False))
        .order_by(PlanBinding.id.desc())
    )


def _run_evaluation(session: Session, binding: PlanBinding) -> PlanEvaluation:
    """Evaluate the bound batch against the bound (frozen) version and store
    the conclusion append-only."""
    batch = session.get(Batch, binding.batch_id)
    result = plan_eval.evaluate_plan(
        _samples_as_dicts(batch),
        _events_as_dicts(batch, include_history=False),
        binding.plan_version.content,
    )
    row = PlanEvaluation(
        binding_id=binding.id,
        review_state="current",
        result=result,
        content_sha256=binding.plan_version.content_sha256,
        anchor_fingerprint=plan_eval.anchor_fingerprint(
            _events_as_dicts(batch, include_history=True)
        ),
        created_by="system",
        note="绑定确认版本时自动生成",
    )
    session.add(row)
    session.flush()
    return row


def _batch_plan_section(batch: Batch, session: Session | None) -> dict[str, Any] | None:
    """Stored binding + stored deviation conclusion for a batch series view.

    Everything read here is a *stored* artifact (the frozen version snapshot,
    the stored evaluation) — never a freshly computed judgment, so the page
    shows exactly what was decided at bind/re-review time.
    """
    if session is None:
        return None
    binding = _current_binding(session, batch.id)
    if binding is None:
        return None
    current_eval = next(
        (r for r in reversed(binding.evaluations) if r.review_state != "obsolete"),
        None,
    )
    live_fp = plan_eval.anchor_fingerprint(
        _events_as_dicts(batch, include_history=True)
    )
    stale = bool(current_eval and current_eval.anchor_fingerprint != live_fp)
    return {
        "binding": _binding_out(binding),
        "current_evaluation": _evaluation_out(current_eval) if current_eval else None,
        "stale": stale,
        "stale_reason": (
            "绑定后锚点事件（回温点/一爆/出锅等）被人工修正，"
            "存储的方案判断需要重新审阅；下方仍保留原判断与依据。"
            if stale
            else None
        ),
    }


def _mark_plan_evaluations_for_review(session: Session, batch_id: int) -> None:
    """After an anchor event correction, flag stored plan judgments.

    The old evaluation row is not deleted or rewritten: its result/evidence
    stay readable, and the review panel can re-run evaluation into a new row.
    """
    binding = _current_binding(session, batch_id)
    if binding is None:
        return
    for r in binding.evaluations:
        if r.review_state == "current":
            r.review_state = "needs_review"


def _get_plan(session: Session, plan_id: int) -> RoastPlan:
    p = session.get(RoastPlan, plan_id)
    if p is None:
        raise HTTPException(404, f"plan {plan_id} not found")
    return p


def _head_version(session: Session, plan_id: int) -> RoastPlanVersion:
    v = session.scalar(
        select(RoastPlanVersion)
        .where(RoastPlanVersion.plan_id == plan_id)
        .order_by(RoastPlanVersion.version_no.desc())
    )
    if v is None:
        raise HTTPException(404, f"plan {plan_id} has no versions")
    return v


def _commit_or_conflict(session: Session, message: str) -> None:
    """Commit, turning a unique-constraint race into an explicit 409."""
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, f"{message}（{exc.orig}）")


# ---------------------------------------------------------------------------
# batches / seeding
# ---------------------------------------------------------------------------

@app.get("/api/batches", response_model=list[BatchMeta])
def list_batches() -> list[Batch]:
    with Session(engine) as s:
        return list(s.scalars(select(Batch).order_by(Batch.id)))


@app.post("/api/batches", response_model=BatchMeta, status_code=201)
def create_batch(body: BatchCreate) -> Batch:
    """Manual batch entry (raw samples + sourced events).

    Like seeding, samples are stored verbatim — NULL bean temperatures stay
    NULL (probe loss), they are never filled here.
    """
    from datetime import datetime as _dt

    with Session(engine) as s:
        if s.scalar(select(Batch).where(Batch.name == body.name)):
            raise HTTPException(409, f"批次名已存在：{body.name}")
        b = Batch(
            name=body.name,
            roaster=body.roaster,
            bean=body.bean,
            charge_at=body.charge_at or _dt.utcnow(),
            charge_temp_c=(
                body.charge_temp_c
                if body.charge_temp_c is not None
                else next(
                    (x.bean_temp_c for x in sorted(body.samples, key=lambda z: z.t_s)
                     if x.bean_temp_c is not None),
                    0.0,
                )
            ),
            ambient_temp_c=body.ambient_temp_c,
            target_drop_temp_c=body.target_drop_temp_c,
            note=body.note,
        )
        b.samples = [
            Sample(
                t_s=sp.t_s,
                bean_temp_c=sp.bean_temp_c,
                env_temp_c=sp.env_temp_c,
                sampled_at=sp.sampled_at,
            )
            for sp in body.samples
        ]
        b.events = [
            Event(**ev.model_dump()) for ev in body.events
        ]
        s.add(b)
        _commit_or_conflict(s, "批次名已存在")
        s.refresh(b)
        return b


@app.post("/api/seed", response_model=list[BatchMeta])
def seed_demo() -> list[Batch]:
    """Load the two synthetic demo batches (noise + dropouts, no machine)."""
    with Session(engine) as s:
        created: list[Batch] = []
        for spec in synth.two_demo_batches():
            existing = s.scalar(select(Batch).where(Batch.name == spec["name"]))
            if existing is not None:
                created.append(existing)
                continue
            b = Batch(
                name=spec["name"],
                roaster=spec["roaster"],
                bean=spec["bean"],
                charge_at=spec["charge_at"],
                charge_temp_c=spec["charge_temp_c"],
                ambient_temp_c=spec["ambient_temp_c"],
                target_drop_temp_c=spec["target_drop_temp_c"],
                note=spec["note"],
            )
            b.samples = [
                Sample(
                    t_s=sp["t_s"],
                    bean_temp_c=sp["bean_temp_c"],
                    env_temp_c=sp["env_temp_c"],
                )
                for sp in spec["samples"]
            ]
            b.events = [Event(**ev) for ev in spec["events"]]
            s.add(b)
            created.append(b)
        s.commit()
        for b in created:
            s.refresh(b)
        return created


@app.get("/api/batches/{batch_id}/series")
def get_series(
    batch_id: int,
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
    include_history: bool = Query(False),
) -> dict[str, Any]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        return _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=max_gap_fill_s,
            include_history=include_history,
            session=s,
        )


# ---------------------------------------------------------------------------
# events: append-only corrections with provenance
# ---------------------------------------------------------------------------

@app.post("/api/batches/{batch_id}/events", response_model=EventOut)
def add_event(batch_id: int, ev: EventIn) -> Event:
    with Session(engine) as s:
        _get_batch(s, batch_id)
        row = Event(batch_id=batch_id, **ev.model_dump())
        s.add(row)
        s.flush()
        # Only one *current* event per type: supersede the previous current one.
        if row.event_type != "damper_change":
            prev = s.scalars(
                select(Event).where(
                    Event.batch_id == batch_id,
                    Event.event_type == row.event_type,
                    Event.superseded.is_(False),
                    Event.id != row.id,
                )
            ).all()
            for p in prev:
                p.superseded = True
                p.superseded_by_id = row.id
        # A correction to an anchor event changes the basis of any stored plan
        # judgment: flag it for re-review (the stored row itself is preserved).
        if row.event_type in plan_eval.PLAN_ANCHORS:
            _mark_plan_evaluations_for_review(s, batch_id)
        s.commit()
        s.refresh(row)
        return row


@app.get("/api/batches/{batch_id}/events", response_model=list[EventOut])
def list_events(batch_id: int, include_history: bool = Query(False)) -> list[Event]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        q = select(Event).where(Event.batch_id == batch_id)
        if not include_history:
            q = q.where(Event.superseded.is_(False))
        return list(s.scalars(q.order_by(Event.t_s)))


# ---------------------------------------------------------------------------
# comparison (no causal claims) + export / recompute
# ---------------------------------------------------------------------------

@app.get("/api/compare")
def compare(
    a: int = Query(..., description="first batch id"),
    b: int = Query(..., description="second batch id"),
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
) -> dict[str, Any]:
    """Overlay two batches on charge-relative time. Damper changes are shown
    as marks so the operator can eyeball before/after shape; the API attaches
    an explicit non-causal note."""
    with Session(engine) as s:
        ba, bb = _get_batch(s, a), _get_batch(s, b)
        payload = {
            "batches": [
                _series_payload(
                    ba,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                    session=s,
                ),
                _series_payload(
                    bb,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                    session=s,
                ),
            ],
            "interpretation": (
                "曲线按开火/下豆时刻对齐叠加。风门变化以标记线显示，"
                "前后形态仅供观察对比，不构成因果结论（无对照、无重复、无统计检验）。"
            ),
        }
        return payload


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: int, window_s: float = 30.0, display_smooth_s: float = 12.0) -> dict[str, Any]:
    """Self-contained export: raw samples, sourced events, parameters, the
    derived phase metrics, and — when the batch is bound — an immutable
    **plan snapshot + stored deviation conclusion**.  The metrics and the
    plan judgment can be reproduced from raw + events + the snapshots
    (see /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        payload = _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
            session=s,
        )
        payload["export_version"] = 2
        plan_section = payload.get("plan")
        if plan_section is not None:
            binding = plan_section["binding"]
            ver = binding["plan_version"]
            cur_eval = plan_section["current_evaluation"]
            payload["plan_snapshot"] = {
                "plan_id": binding["plan_id"],
                "plan_version_id": ver["id"],
                "version_no": ver["version_no"],
                "status_at_export": ver["status"],
                "content": ver["content"],
                "content_sha256": ver["content_sha256"],
            }
            if cur_eval is not None:
                payload["plan_evaluation"] = {
                    "evaluation_id": cur_eval["id"],
                    "review_state": cur_eval["review_state"],
                    "result": cur_eval["result"],
                    "content_sha256": cur_eval["content_sha256"],
                    "anchor_fingerprint": cur_eval["anchor_fingerprint"],
                    "created_at": cur_eval["created_at"],
                }
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "metrics_depend_on": ["raw_samples", "current(non-superseded) events", "ror_window_s"],
            "plan_judgment_depends_on": [
                "raw_samples",
                "current(non-superseded) anchor events",
                "frozen plan_snapshot.content",
            ],
            "pipeline": (
                "numpy centred least-squares RoR; linear gap fill flagged; "
                "plan deviation evaluated on measured points only"
            ),
        }
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics (+ optional plan judgment) from an
    export-style payload, without touching the database.

    Body: {"samples": [...], "events": [...], "params": {...},
           "plan_snapshot": {"content": ...}}.  When a plan snapshot is
    present, ``recompute_plan_evaluation`` reproduces the per-segment
    deviations/unevaluable reasons from the frozen content, so an old export
    yields the same judgment even after the plan has new versions.
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        params = payload.get("params", {})
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}")
    cfg = RoRConfig(
        window_s=float(params.get("ror_window_s", 30.0)),
        display_smooth_s=float(params.get("ror_display_smooth_s", 12.0)),
    )
    series = build_series(
        samples,
        ror_cfg=cfg,
        max_gap_fill_s=float(params.get("max_gap_fill_s", MAX_GAP_FILL_S)),
    )
    out: dict[str, Any] = {
        "series": series,
        "metrics": phase_metrics(events),
        "current_events": current_events(events),
    }
    snap = payload.get("plan_snapshot")
    if snap is not None:
        try:
            out["recompute_plan_evaluation"] = {
                "result": plan_eval.evaluate_plan(samples, events, snap["content"]),
                "content_sha256": plan_eval.content_sha256(
                    plan_eval.validate_plan_content(snap["content"])
                ),
            }
        except plan_eval.PlanContentError as exc:
            raise HTTPException(422, f"plan_snapshot.content 无效：{exc}")
    return out


# ---------------------------------------------------------------------------
# roasting plans: versions, lifecycle, bindings, stored evaluations
# ---------------------------------------------------------------------------

@app.get("/api/plans")
def list_plans(include_versions: bool = Query(True)) -> list[dict[str, Any]]:
    with Session(engine) as s:
        plans = list(s.scalars(select(RoastPlan).order_by(RoastPlan.id)))
        if not include_versions:
            return [
                {k: v for k, v in _plan_out(p).items() if k != "versions"}
                for p in plans
            ]
        return [_plan_out(p) for p in plans]


@app.post("/api/plans")
def create_plan(body: PlanCreate) -> dict[str, Any]:
    with Session(engine) as s:
        if s.scalar(select(RoastPlan).where(RoastPlan.name == body.name)):
            raise HTTPException(409, f"方案名已存在：{body.name}")
        try:
            content = plan_eval.validate_plan_content(body.content)
        except plan_eval.PlanContentError as exc:
            raise HTTPException(422, str(exc))
        p = RoastPlan(
            name=body.name,
            bean=body.bean,
            note=body.note,
            created_by=body.created_by,
        )
        s.add(p)
        s.flush()
        s.add(
            RoastPlanVersion(
                plan_id=p.id,
                version_no=1,
                status="draft",
                content=content,
                content_sha256=plan_eval.content_sha256(content),
                change_note=body.change_note,
                created_by=body.created_by,
                base_version_no=None,
            )
        )
        _commit_or_conflict(s, "方案名已存在")
        s.refresh(p)
        return _plan_out(p)


@app.post("/api/plans/seed-default")
def seed_default_plan() -> dict[str, Any]:
    """Create (idempotently) the built-in draft template plan."""
    with Session(engine) as s:
        existing = s.scalar(select(RoastPlan).where(RoastPlan.name == DEFAULT_PLAN_NAME))
        if existing is not None:
            return _plan_out(existing)
        content = plan_eval.validate_plan_content(DEFAULT_PLAN_CONTENT)
        p = RoastPlan(name=DEFAULT_PLAN_NAME, bean="通用/埃塞风格", note="内置可复审模板")
        s.add(p)
        s.flush()
        s.add(
            RoastPlanVersion(
                plan_id=p.id,
                version_no=1,
                status="draft",
                content=content,
                content_sha256=plan_eval.content_sha256(content),
                change_note="内置模板初稿（草稿，确认前不可绑定）",
            )
        )
        _commit_or_conflict(s, "方案已存在或版本号冲突")
        s.refresh(p)
        return _plan_out(p)


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        return _plan_out(_get_plan(s, plan_id))


@app.post("/api/plans/{plan_id}/versions")
def create_plan_version(plan_id: int, body: PlanVersionCreate) -> dict[str, Any]:
    """Append a new immutable version.

    Optimistic concurrency: ``base_version_no`` must be the current HEAD.
    Two editors editing the same old version produce two submits; the later
    one gets an explicit 409 conflict and must rebase instead of overwriting
    the first submit.
    """
    with Session(engine) as s:
        p = _get_plan(s, plan_id)
        head = _head_version(s, plan_id)
        if body.base_version_no != head.version_no:
            raise HTTPException(
                409,
                f"版本冲突：您基于 v{body.base_version_no} 提交，但方案 HEAD 已是 "
                f"v{head.version_no}（状态 {head.status}）。请基于最新版本重新提交改动，"
                "先到版本不会被覆盖。",
            )
        try:
            content = plan_eval.validate_plan_content(body.content)
        except plan_eval.PlanContentError as exc:
            raise HTTPException(422, str(exc))
        v = RoastPlanVersion(
            plan_id=p.id,
            version_no=head.version_no + 1,
            status="draft",
            content=content,
            content_sha256=plan_eval.content_sha256(content),
            change_note=body.change_note,
            created_by=body.created_by,
            base_version_no=body.base_version_no,
        )
        s.add(v)
        _commit_or_conflict(s, f"版本冲突：v{head.version_no + 1} 已被并行提交，请基于最新 HEAD 重新提交")
        s.refresh(v)
        return _version_out(v)


@app.post("/api/plans/{plan_id}/versions/{version_no}/confirm")
def confirm_plan_version(
    plan_id: int, version_no: int, body: PlanStatusUpdate
) -> dict[str, Any]:
    """Confirm a version.  Only the HEAD version can be confirmed; confirming
    auto-retires the previously confirmed version (content stays immutable)."""
    with Session(engine) as s:
        _get_plan(s, plan_id)
        v = s.scalar(
            select(RoastPlanVersion).where(
                RoastPlanVersion.plan_id == plan_id,
                RoastPlanVersion.version_no == version_no,
            )
        )
        if v is None:
            raise HTTPException(404, f"plan {plan_id} v{version_no} not found")
        head = _head_version(s, plan_id)
        if v.id != head.id:
            raise HTTPException(
                409,
                f"只能确认最新版本：当前 HEAD 为 v{head.version_no}，"
                f"v{version_no} 已不是 HEAD。",
            )
        if v.status != "draft":
            raise HTTPException(409, f"v{version_no} 状态为 {v.status}，只有草稿可以确认")
        if body.expected_status is not None and body.expected_status != "draft":
            raise HTTPException(409, "expected_status 与当前状态不一致")
        for old in s.scalars(
            select(RoastPlanVersion).where(
                RoastPlanVersion.plan_id == plan_id,
                RoastPlanVersion.status == "confirmed",
            )
        ):
            old.status = "retired"
            old.retired_at = datetime.utcnow()
        v.status = "confirmed"
        v.confirmed_at = datetime.utcnow()
        s.commit()
        s.refresh(v)
        return _version_out(v)


@app.post("/api/plans/{plan_id}/versions/{version_no}/retire")
def retire_plan_version(
    plan_id: int, version_no: int, body: PlanStatusUpdate
) -> dict[str, Any]:
    """Retire a confirmed version (content stays frozen; bindings and exports
    keep referencing it).  Retiring the confirmed HEAD frees a new draft to be
    confirmed."""
    with Session(engine) as s:
        _get_plan(s, plan_id)
        v = s.scalar(
            select(RoastPlanVersion).where(
                RoastPlanVersion.plan_id == plan_id,
                RoastPlanVersion.version_no == version_no,
            )
        )
        if v is None:
            raise HTTPException(404, f"plan {plan_id} v{version_no} not found")
        if v.status != "confirmed":
            raise HTTPException(409, f"v{version_no} 状态为 {v.status}，只有已确认版本可以退役")
        if body.expected_status is not None and body.expected_status != "confirmed":
            raise HTTPException(409, "expected_status 与当前状态不一致")
        v.status = "retired"
        v.retired_at = datetime.utcnow()
        s.commit()
        s.refresh(v)
        return _version_out(v)


@app.post("/api/batches/{batch_id}/bind-plan")
def bind_batch_to_plan(batch_id: int, body: PlanBindingCreate) -> dict[str, Any]:
    """Bind a batch to one *confirmed* immutable version and compute/store its
    initial deviation conclusion.  Rebinding supersedes the previous binding
    (it stays in history); the old evaluation rows remain readable."""
    with Session(engine) as s:
        _get_batch(s, batch_id)
        ver = s.get(RoastPlanVersion, body.plan_version_id)
        if ver is None:
            raise HTTPException(404, f"plan version {body.plan_version_id} not found")
        if ver.status != "confirmed":
            raise HTTPException(
                409,
                f"只能绑定已确认版本：v{ver.version_no} 当前为 {ver.status}。",
            )
        prev = _current_binding(s, batch_id)
        binding = PlanBinding(
            batch_id=batch_id,
            plan_id=ver.plan_id,
            plan_version_id=ver.id,
            created_by=body.created_by,
        )
        s.add(binding)
        s.flush()
        if prev is not None:
            prev.superseded = True
            prev.superseded_by_id = binding.id
            for r in prev.evaluations:
                if r.review_state in ("current", "needs_review"):
                    r.review_state = "obsolete"
        _run_evaluation(s, binding)
        _commit_or_conflict(s, "该批次已有并行绑定提交，请刷新后重试")
        s.refresh(binding)
        return _binding_out(binding)


@app.get("/api/batches/{batch_id}/bindings")
def list_bindings(
    batch_id: int, include_evaluations: bool = Query(True)
) -> list[dict[str, Any]]:
    """Binding history (old, superseded bindings included) with all stored
    evaluations — this is how historical judgments and their evidence remain
    reviewable after rebinding or event corrections."""
    with Session(engine) as s:
        _get_batch(s, batch_id)
        rows = list(
            s.scalars(
                select(PlanBinding)
                .where(PlanBinding.batch_id == batch_id)
                .order_by(PlanBinding.id)
            )
        )
        out = []
        for b in rows:
            d = _binding_out(b)
            if not include_evaluations:
                d.pop("evaluations")
            out.append(d)
        return out


@app.post("/api/batches/{batch_id}/plan-review")
def rerun_plan_review(batch_id: int, body: ReReviewRequest) -> dict[str, Any]:
    """Re-run the plan judgment for the current binding (append-only).

    Used after a manual anchor correction marked the stored conclusion
    needs_review: the old row is marked obsolete but its result/evidence stay
    in the binding history; a new row carries the fresh judgment.
    """
    with Session(engine) as s:
        _get_batch(s, batch_id)
        binding = _current_binding(s, batch_id)
        if binding is None:
            raise HTTPException(409, "该批次尚未绑定任何已确认方案版本")
        for r in binding.evaluations:
            if r.review_state in ("current", "needs_review"):
                r.review_state = "obsolete"
        row = _run_evaluation(s, binding)
        row.created_by = body.created_by or "operator"
        row.note = body.note or "人工触发重新审阅"
        row.reviewed_at = datetime.utcnow()
        s.commit()
        s.refresh(row)
        return _evaluation_out(row)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}
