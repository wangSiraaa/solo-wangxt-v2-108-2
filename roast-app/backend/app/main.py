"""FastAPI application: batch curves, sourced events, comparison, export."""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import plan_api, synth
from .analysis import RoRConfig, build_series, current_events, phase_metrics
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .models import ASSESSMENT_NEEDS_REVIEW, Batch, Event, Sample, engine, init_db
from .schemas import BatchMeta, EventIn, EventOut

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(plan_api.router)


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
    session: Session,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
) -> dict[str, Any]:
    series = build_series(
        _samples_as_dicts(batch),
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
    )
    events = _events_as_dicts(batch, include_history=include_history)
    payload = {
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
    payload["plan"] = _plan_block(session, batch)
    return payload


def _plan_block(session: Session, batch: Batch) -> dict[str, Any] | None:
    """Binding + immutable version snapshot + latest persistent judgement.

    The version definition is embedded as a SNAPSHOT: later edits/new versions
    never mutate what an already-bound batch or an old export was judged with.
    """
    binding = plan_api.current_binding(session, batch.id)
    if binding is None:
        return None
    version = session.get(plan_api.RoastPlanVersion, binding.plan_version_id)
    plan = session.get(plan_api.RoastPlan, version.plan_id)
    assessment = plan_api.current_assessment(session, batch.id)
    block = {
        "binding": {
            "id": binding.id,
            "plan_version_id": binding.plan_version_id,
            "bound_by": binding.bound_by,
            "note": binding.note,
            "created_at": binding.created_at.isoformat(),
        },
        "version_snapshot": {
            "plan_id": plan.id,
            "plan_name": plan.name,
            "version_id": version.id,
            "version_no": version.version_no,
            "status": version.status,
            "content_hash": version.content_hash,
            "definition": plan_api._definition(version),
            "confirmed_at": version.confirmed_at.isoformat() if version.confirmed_at else None,
            "snapshot_note": "已确认版本内容不可变；本快照即该批次判断依据，不随后续版本修改。",
        },
        "assessment": plan_api.assessment_out(assessment) if assessment else None,
        "disclaimer": plan_api.plan_lib.NON_CAUSAL_DISCLAIMER,
    }
    return block


# ---------------------------------------------------------------------------
# batches / seeding
# ---------------------------------------------------------------------------

@app.get("/api/batches", response_model=list[BatchMeta])
def list_batches() -> list[Batch]:
    with Session(engine) as s:
        return list(s.scalars(select(Batch).order_by(Batch.id)))


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
            s,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=max_gap_fill_s,
            include_history=include_history,
        )


# ---------------------------------------------------------------------------
# events: append-only corrections with provenance
# ---------------------------------------------------------------------------

@app.post("/api/batches/{batch_id}/events")
def add_event(batch_id: int, ev: EventIn) -> dict[str, Any]:
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
        # Anchor corrections invalidate plan judgements: flag, don't overwrite.
        flagged = plan_api.mark_stale_for_anchor_event(
            s, batch_id, row.event_type, row.t_s
        )
        s.commit()
        s.refresh(row)
        out = EventOut.model_validate(row).model_dump(mode="json")
        if flagged:
            stale = flagged[0]
            out["plan_review"] = {
                "flagged": True,
                "review_state": ASSESSMENT_NEEDS_REVIEW,
                "reason": stale.needs_review_reason,
                "assessment_id": stale.id,
            }
        else:
            out["plan_review"] = {"flagged": False}
        return out


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
                    s,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
                _series_payload(
                    bb,
                    s,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
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
    """Self-contained export: raw samples, sourced events, parameters, and the
    derived phase metrics.  The metrics can be reproduced from raw + events +
    the stated window (see /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        payload = _series_payload(
            b,
            s,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
        )
        # Export carries the full plan SNAPSHOT plus every stored judgement
        # (including needs_review / superseded rows) so a third party can
        # reproduce exactly the verdict the batch was bound to, even after the
        # plan gains newer versions or the anchor events are corrected.
        binding = plan_api.current_binding(s, batch_id)
        from sqlalchemy import select as _select
        assessments = list(
            s.scalars(
                _select(plan_api.PlanAssessment)
                .where(plan_api.PlanAssessment.batch_id == batch_id)
                .order_by(plan_api.PlanAssessment.id)
            )
        )
        bindings = list(
            s.scalars(
                _select(plan_api.BatchPlanBinding)
                .where(plan_api.BatchPlanBinding.batch_id == batch_id)
                .order_by(plan_api.BatchPlanBinding.id)
            )
        )
        plan_export = None
        if bindings:
            plan_export = {
                "current_binding_id": binding.id if binding else None,
                "bindings": [
                    {
                        "id": r.id,
                        "plan_version_id": r.plan_version_id,
                        "bound_by": r.bound_by,
                        "note": r.note,
                        "superseded": r.superseded,
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in bindings
                ],
                "version_snapshots": [
                    plan_api.version_out(s.get(plan_api.RoastPlanVersion, r.plan_version_id))
                    for r in bindings
                ],
                "assessments": [plan_api.assessment_out(r) for r in assessments],
                "reproducibility": {
                    "definition_is_immutable_snapshot": True,
                    "assessment_hash_algorithm": "sha256(canonical_json(result))",
                    "recompute_endpoint": "/api/recompute",
                },
                "disclaimer": plan_api.plan_lib.NON_CAUSAL_DISCLAIMER,
            }
        payload["export_version"] = 2
        payload["plan_export"] = plan_export
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "metrics_depend_on": ["raw_samples", "current(non-superseded) events", "ror_window_s"],
            "pipeline": "numpy centred least-squares RoR; linear gap fill flagged",
            "plan_judgement_depends_on": [
                "raw_samples",
                "current anchor events",
                "immutable plan version snapshot",
                "max_gap_fill_s",
            ],
        }
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics (+ plan judgement) from an export payload.

    Used to verify an export reproduces every stage metric and every plan
    segment verdict without touching the database.  Body::

        {"samples": [...], "events": [...], "params": {...},
         "plan_definition": {...}          # optional, from the version snapshot
        }
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
    max_gap = float(params.get("max_gap_fill_s", MAX_GAP_FILL_S))
    series = build_series(samples, ror_cfg=cfg, max_gap_fill_s=max_gap)
    out = {
        "series": series,
        "metrics": phase_metrics(events),
        "current_events": current_events(events),
    }
    definition = payload.get("plan_definition")
    if definition is not None:
        from . import plans as plan_lib
        result = plan_lib.evaluate_plan(
            definition,
            plan_lib.attach_guide(series["raw_points"], series["guide_bean_temp"]),
            events,
            max_gap_fill_s=max_gap,
            missing_segments=series["missing_segments"],
        )
        out["plan_assessment"] = result
    return out


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}
