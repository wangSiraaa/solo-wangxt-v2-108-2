"""Roast plan versions: definitions, hashing, and anchor-aligned evaluation.

A *plan* is a target curve built entirely **relative to defined anchors**
(charge, turning point, first crack start, drop ...).  Its definition is a JSON
document::

    {
      "anchor_schedule": {"charge": 0},   # optional absolute anchor overrides
      "segments": [
        {
          "id": "drying",
          "from_anchor": "charge",
          "to_anchor": "turning_point",
          "target_duration_s": 75,
          "duration_tolerance_s": 20,
          "end_temp_c": 104,            # optional temperature checks
          "end_temp_tolerance_c": 10,
          "shape": [                     # optional interior check points
            {"offset_frac": 0.5, "target_temp_c": 120, "tolerance_c": 8}
          ],
        },
        ...
      ],
    }

Every derived number here is a **pure function** of (definition, measured
samples, current events).  Nothing is written back, and the evaluation is
deliberately conservative about data quality:

* a segment whose boundary anchor is missing is ``unevaluable`` — no verdict,
  no fabricated pass;
* a segment that crosses a wide, unfilled probe gap is ``unevaluable`` —
  bridging the gap would turn missing data into a conclusion;
* an individual temperature check landing on an interpolated point or an
  unfilled gap is ``unevaluable`` and states so; neighbouring interpolated
  points are NEVER counted as passing evidence;
* only measured samples within an explicit window of the check time count.

The output therefore never claims more than the observations support, and it
never interprets deviation as causation or as a product-quality verdict — the
caller is responsible for attaching that disclaimer.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

# Anchor event types a plan may reference.  charge is always the t=0 origin.
PLAN_ANCHORS = ("charge", "turning_point", "first_crack_start", "drop")

# Segment ordering is fixed by the roast; a plan may omit later segments but
# cannot reorder them.
SEGMENT_ORDER = {
    "drying": ("charge", "turning_point"),
    "maillard": ("turning_point", "first_crack_start"),
    "development": ("first_crack_start", "drop"),
}

# Verdict vocabulary.
V_PASS = "pass"
V_FAIL = "fail"
V_UNEVALUABLE = "unevaluable"
OVERALL_PASS = "all_within_tolerance"
OVERALL_FAIL = "deviation_outside_tolerance"
OVERALL_UNEVALUABLE = "unevaluable_segments"
OVERALL_MIXED = "partial_deviation"

# A temperature check uses the nearest measured sample within this window.
TEMP_EVIDENCE_WINDOW_S = 8.0
ASSESSMENT_FORMAT_VERSION = 1

NON_CAUSAL_DISCLAIMER = (
    "本结论仅描述实际曲线与已确认烘焙方案版本之间的过程偏差。"
    "未评估段不代表通过；相邻插值不作为通过依据。"
    "过程偏差不构成因果判断，也不构成成品质量结论（无对照、无重复、无统计检验）。"
)


class PlanDefinitionError(ValueError):
    """Raised when a plan definition document is structurally invalid."""


# ---------------------------------------------------------------------------
# canonical form + hashing
# ---------------------------------------------------------------------------

def canonical_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Return the normalised definition, or raise ``PlanDefinitionError``.

    The canonical form is what gets hashed and stored, so two semantically
    equal documents hash identically regardless of key order/whitespace.
    """
    if not isinstance(definition, dict):
        raise PlanDefinitionError("definition must be an object")
    raw_segments = definition.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise PlanDefinitionError("definition.segments must be a non-empty list")

    seen_ids: set[str] = set()
    segments: list[dict[str, Any]] = []
    last_order = -1
    for i, seg in enumerate(raw_segments):
        if not isinstance(seg, dict):
            raise PlanDefinitionError(f"segment #{i} must be an object")
        seg_id = seg.get("id")
        if not seg_id or not isinstance(seg_id, str):
            raise PlanDefinitionError(f"segment #{i}: id is required")
        if seg_id in seen_ids:
            raise PlanDefinitionError(f"duplicate segment id: {seg_id}")
        seen_ids.add(seg_id)

        pair = SEGMENT_ORDER.get(seg_id)
        if pair is None:
            raise PlanDefinitionError(
                f"segment {seg_id}: unknown segment; expected one of "
                f"{', '.join(SEGMENT_ORDER)}"
            )
        fa = seg.get("from_anchor", pair[0])
        ta = seg.get("to_anchor", pair[1])
        if fa != pair[0] or ta != pair[1]:
            raise PlanDefinitionError(
                f"segment {seg_id}: must run {pair[0]} -> {pair[1]}"
            )
        order = list(SEGMENT_ORDER).index(seg_id)
        if order <= last_order:
            raise PlanDefinitionError(
                "segments must follow the roast order "
                "drying -> maillard -> development without repetition"
            )
        last_order = order

        norm: dict[str, Any] = {
            "id": seg_id,
            "from_anchor": pair[0],
            "to_anchor": pair[1],
        }
        td = seg.get("target_duration_s")
        tol = seg.get("duration_tolerance_s")
        if td is None:
            raise PlanDefinitionError(f"segment {seg_id}: target_duration_s is required")
        norm["target_duration_s"] = _num(td, f"segment {seg_id}.target_duration_s", pos=True)
        if tol is None:
            raise PlanDefinitionError(
                f"segment {seg_id}: duration_tolerance_s is required"
            )
        norm["duration_tolerance_s"] = _num(
            tol, f"segment {seg_id}.duration_tolerance_s", nonneg=True
        )

        if "end_temp_c" in seg and seg["end_temp_c"] is not None:
            norm["end_temp_c"] = _num(seg["end_temp_c"], f"segment {seg_id}.end_temp_c")
            et = seg.get("end_temp_tolerance_c")
            if et is None:
                raise PlanDefinitionError(
                    f"segment {seg_id}: end_temp_tolerance_c required with end_temp_c"
                )
            norm["end_temp_tolerance_c"] = _num(
                et, f"segment {seg_id}.end_temp_tolerance_c", nonneg=True
            )

        shape = seg.get("shape") or []
        if not isinstance(shape, list):
            raise PlanDefinitionError(f"segment {seg_id}: shape must be a list")
        norm_shape: list[dict[str, Any]] = []
        prev_off = 0.0
        for j, pt in enumerate(shape):
            off = _num(pt.get("offset_frac"), f"segment {seg_id}.shape[{j}].offset_frac")
            if not (0.0 < off < 1.0):
                raise PlanDefinitionError(
                    f"segment {seg_id}.shape[{j}]: offset_frac must be in (0,1)"
                )
            if off <= prev_off:
                raise PlanDefinitionError(
                    f"segment {seg_id}.shape[{j}]: offsets must be strictly increasing"
                )
            prev_off = off
            tgt = _num(pt.get("target_temp_c"), f"segment {seg_id}.shape[{j}].target_temp_c")
            tol_c = _num(
                pt.get("tolerance_c"), f"segment {seg_id}.shape[{j}].tolerance_c", nonneg=True
            )
            norm_shape.append(
                {"offset_frac": off, "target_temp_c": tgt, "tolerance_c": tol_c}
            )
        norm["shape"] = norm_shape

        # Every segment must contain at least one check; duration always is one.
        segments.append(norm)

    schedule = definition.get("anchor_schedule") or {}
    if not isinstance(schedule, dict):
        raise PlanDefinitionError("anchor_schedule must be an object")
    norm_schedule: dict[str, float] = {}
    for k, v in schedule.items():
        if k not in PLAN_ANCHORS:
            raise PlanDefinitionError(f"anchor_schedule: unknown anchor {k}")
        norm_schedule[k] = _num(v, f"anchor_schedule.{k}", nonneg=True)
    # charge is the time origin by definition.
    norm_schedule.setdefault("charge", 0.0)

    return {"anchor_schedule": norm_schedule, "segments": segments}


def _num(v: Any, where: str, *, pos: bool = False, nonneg: bool = False) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise PlanDefinitionError(f"{where}: expected a number, got {v!r}")
    if math.isnan(x) or math.isinf(x):
        raise PlanDefinitionError(f"{where}: finite number required")
    if pos and x <= 0:
        raise PlanDefinitionError(f"{where}: must be > 0")
    if nonneg and x < 0:
        raise PlanDefinitionError(f"{where}: must be >= 0")
    return round(x, 6)


def canonical_json(definition: dict[str, Any]) -> str:
    return json.dumps(
        canonical_definition(definition),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(definition: dict[str, Any]) -> str:
    """Stable SHA-256 over the canonical definition (identity of content)."""
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Point:
    __slots__ = ("t", "temp", "interp")
    t: float
    temp: float | None
    interp: bool


def attach_guide(
    raw_points: list[dict], guide_bean_temp: list[float | None] | None
) -> list[dict]:
    """Copy raw points, attaching the flagged linear-fill value (if any) as
    ``guide_bean_temp_c``.  The measured value is never altered; the guide is
    only used to RECOGNISE interpolated points so they cannot serve as
    measured evidence."""
    if guide_bean_temp is None:
        return list(raw_points)
    out = []
    for i, p in enumerate(raw_points):
        q = dict(p)
        if (
            p.get("bean_temp_c") is None
            and p.get("is_interpolated")
            and i < len(guide_bean_temp)
            and guide_bean_temp[i] is not None
        ):
            q["guide_bean_temp_c"] = guide_bean_temp[i]
        out.append(q)
    return out


def _points(raw_points: list[dict]) -> list[_Point]:
    out = []
    for p in sorted(raw_points, key=lambda p: p["t_s"]):
        bean = p.get("bean_temp_c")
        if bean is None:
            guide = p.get("guide_bean_temp_c")
            if bool(p.get("is_interpolated")) and guide is not None:
                # Bridged by the plotting fill: present as a point but marked
                # non-measured, so it can never count as pass evidence.
                out.append(_Point(float(p["t_s"]), float(guide), True))
            else:
                out.append(_Point(float(p["t_s"]), None, False))
        else:
            out.append(
                _Point(
                    float(p["t_s"]),
                    float(bean),
                    bool(p.get("is_interpolated", False)),
                )
            )
    return out


def _wide_gaps(missing_segments: list[dict]) -> list[tuple[float, float]]:
    return [
        (float(g["t_start_s"]), float(g["t_end_s"]))
        for g in missing_segments
        if g.get("channel", "bean") == "bean" and g.get("status") == "wide_unfilled"
    ]


def _crosses_gap(t0: float, t1: float, gaps: list[tuple[float, float]], eps: float = 1e-6) -> dict | None:
    lo, hi = sorted((t0, t1))
    for gs, ge in gaps:
        # A gap strictly inside the open segment blocks it.  A boundary anchor
        # sitting exactly at the gap edge is a check-level problem, handled by
        # the evidence lookup instead.
        if gs > lo + eps and ge < hi - eps:
            return {"t_start_s": gs, "t_end_s": ge}
    return None


def _temperature_evidence(
    t_check: float, points: list[_Point]
) -> tuple[str, dict | None]:
    """Find what supports a temperature claim at ``t_check``.

    Returns ("measured", {"t_s", "bean_temp_c", "distance_s"}) when a measured
    (never interpolated) sample lies within ``TEMP_EVIDENCE_WINDOW_S``.
    Otherwise returns ("unevaluable", {"reason_code", "reason_zh", ...}) and
    the caller must NOT treat interpolation as pass evidence.
    """
    # nearest measured (is_interpolated == False) point
    best_measured = None
    best_md = float("inf")
    best_interp = None
    best_id = float("inf")
    for p in points:
        if p.temp is None:
            continue
        d = abs(p.t - t_check)
        if p.interp:
            if d < best_id:
                best_id = d
                best_interp = p
        elif d < best_md:
            best_md = d
            best_measured = p
    if best_measured is not None and best_md <= TEMP_EVIDENCE_WINDOW_S:
        return (
            "measured",
            {
                "t_s": round(best_measured.t, 3),
                "bean_temp_c": round(best_measured.temp, 3),
                "distance_s": round(best_md, 3),
            },
        )
    if best_measured is None and best_interp is None:
        return ("unevaluable", {
            "reason_code": "no_samples_in_window",
            "reason_zh": f"目标时刻 ±{TEMP_EVIDENCE_WINDOW_S:.0f}s 内没有任何采样（端点/长缺测），无法判断",
        })
    if best_md > TEMP_EVIDENCE_WINDOW_S and best_interp is not None and best_id <= TEMP_EVIDENCE_WINDOW_S:
        return ("unevaluable", {
            "reason_code": "only_interpolated_in_window",
            "reason_zh": (
                f"目标时刻 ±{TEMP_EVIDENCE_WINDOW_S:.0f}s 内只有插值点（非实测），"
                "相邻插值不作为通过依据"
            ),
        })
    return ("unevaluable", {
        "reason_code": "no_measured_sample_in_window",
        "reason_zh": (
            f"目标时刻 ±{TEMP_EVIDENCE_WINDOW_S:.0f}s 内没有实测豆温点"
            + ("（探针长缺测）" if best_md == float("inf") else "，最近实测点超出取证窗口")
        ),
    })


def _temp_check(
    check_id: str,
    kind: str,
    t_check: float,
    target_c: float,
    tol_c: float,
    points: list[_Point],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": check_id,
        "kind": kind,
        "check_time_s": round(t_check, 3),
        "target_temp_c": target_c,
        "tolerance_c": tol_c,
    }
    source, ev = _temperature_evidence(t_check, points)
    if source != "measured":
        out.update(status=V_UNEVALUABLE, deviation_c=None, within_tolerance=None, evidence=ev)
        return out
    dev = round(ev["bean_temp_c"] - target_c, 3)
    out.update(
        status=V_PASS if abs(dev) <= tol_c + 1e-9 else V_FAIL,
        deviation_c=dev,
        within_tolerance=abs(dev) <= tol_c + 1e-9,
        evidence=ev,
    )
    return out


def evaluate_plan(
    definition: dict[str, Any],
    raw_points: list[dict],
    events: list[dict],
    *,
    max_gap_fill_s: float,
    missing_segments: list[dict] | None = None,
) -> dict[str, Any]:
    """Compute per-segment deviation conclusions for one batch.

    Pure: same inputs -> same output (suitable for export/recompute).  Statuses
    are ``pass`` / ``fail`` / ``unevaluable`` both per check and per segment,
    and every unevaluable result carries an explicit machine-readable reason.
    """
    canon = canonical_definition(definition)
    points = _points(raw_points)
    gaps = _wide_gaps(missing_segments or [])

    # Current (non-superseded) anchor times.
    anchors: dict[str, float | None] = {}
    cur: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (e["t_s"], e.get("id", 0))):
        if not e.get("superseded", False):
            cur[e["event_type"]] = e
    for name in PLAN_ANCHORS:
        if name == "charge":
            ev = cur.get("charge")
            anchors[name] = float(ev["t_s"]) if ev else canon["anchor_schedule"].get("charge", 0.0)
        elif name in canon["anchor_schedule"]:
            anchors[name] = float(canon["anchor_schedule"][name])
        else:
            ev = cur.get(name)
            anchors[name] = None if ev is None else float(ev["t_s"])

    segments_out: list[dict[str, Any]] = []
    n_pass = n_fail = n_unev = 0
    for seg in canon["segments"]:
        sid = seg["id"]
        t0 = anchors[seg["from_anchor"]]
        t1 = anchors[seg["to_anchor"]]
        checks: list[dict[str, Any]] = []

        missing = [
            a for a in (seg["from_anchor"], seg["to_anchor"]) if anchors[a] is None
        ]
        if missing:
            reason = (
                f"缺少锚点 {'、'.join(missing)}：该段无法定位，未评估"
                "（不以相邻插值或相邻段结果代替）"
            )
            segments_out.append({
                "id": sid,
                "from_anchor": seg["from_anchor"],
                "to_anchor": seg["to_anchor"],
                "status": V_UNEVALUABLE,
                "reason_code": "missing_anchor",
                "reason_zh": reason,
                "missing_anchors": missing,
                "start_s": None,
                "end_s": None,
                "observed_duration_s": None,
                "duration_deviation_s": None,
                "checks": [],
                "blocked_by_gap": None,
            })
            n_unev += 1
            continue

        # Duration check
        dur = t1 - t0
        ddev = round(dur - seg["target_duration_s"], 3)
        checks.append({
            "id": f"{sid}:duration",
            "kind": "duration",
            "target_s": seg["target_duration_s"],
            "tolerance_s": seg["duration_tolerance_s"],
            "observed_s": round(dur, 3),
            "deviation_s": ddev,
            "status": V_PASS if abs(ddev) <= seg["duration_tolerance_s"] + 1e-9 else V_FAIL,
            "within_tolerance": abs(ddev) <= seg["duration_tolerance_s"] + 1e-9,
        })

        # Interior shape points (anchor-relative: t = t0 + frac*(t1-t0))
        for j, pt in enumerate(seg["shape"]):
            t_check = t0 + pt["offset_frac"] * (t1 - t0)
            checks.append(
                _temp_check(
                    f"{sid}:shape[{j}]", "shape_temp", t_check,
                    pt["target_temp_c"], pt["tolerance_c"], points,
                )
            )

        # End-anchor temperature
        if "end_temp_c" in seg:
            checks.append(
                _temp_check(
                    f"{sid}:end_temp", "end_temp", t1,
                    seg["end_temp_c"], seg["end_temp_tolerance_c"], points,
                )
            )

        blocked = _crosses_gap(t0, t1, gaps)
        statuses = {c["status"] for c in checks}
        if blocked:
            status = V_UNEVALUABLE
            n_unev += 1
        elif statuses == {V_PASS}:
            status = V_PASS
            n_pass += 1
        elif V_FAIL in statuses:
            status = V_FAIL
            n_fail += 1
        else:  # only pass + unevaluable (no fail, no blocking gap)
            status = V_UNEVALUABLE
            n_unev += 1

        reason_zh = None
        reason_code = None
        if blocked:
            reason_code = "crosses_wide_unfilled_gap"
            reason_zh = (
                f"该段跨越 {blocked['t_start_s']:.0f}–{blocked['t_end_s']:.0f}s 的"
                "长缺测断档（超出插值桥接上限），整段未评估；不以相邻插值或断档两侧"
                "的点拼凑达标结论。"
            )
        elif status == V_UNEVALUABLE:
            bad = [c for c in checks if c["status"] == V_UNEVALUABLE]
            reason_code = "insufficient_measured_evidence"
            reason_zh = "；".join(
                f"{c['id']}: {c['evidence']['reason_zh']}" for c in bad
            )

        segments_out.append({
            "id": sid,
            "from_anchor": seg["from_anchor"],
            "to_anchor": seg["to_anchor"],
            "status": status,
            "reason_code": reason_code,
            "reason_zh": reason_zh,
            "missing_anchors": [],
            "start_s": round(t0, 3),
            "end_s": round(t1, 3),
            "observed_duration_s": round(dur, 3),
            "duration_deviation_s": ddev,
            "checks": checks,
            "blocked_by_gap": blocked,
        })

    if n_fail == 0 and n_unev == 0:
        overall = OVERALL_PASS
        summary = "全部已评估段均在容差内"
    elif n_pass == 0 and n_fail == 0:
        overall = OVERALL_UNEVALUABLE
        summary = "没有任何段可评估（锚点缺失或数据不足）"
    elif n_fail == 0:
        overall = OVERALL_UNEVALUABLE
        summary = f"{n_pass} 段在容差内，{n_unev} 段无法评估（不视为通过）"
    elif n_pass == 0 and n_unev == 0:
        overall = OVERALL_FAIL
        summary = f"全部 {n_fail} 个已评估段超出容差"
    else:
        overall = OVERALL_MIXED
        parts = [f"{n_pass} 段在容差内", f"{n_fail} 段超出容差"]
        if n_unev:
            parts.append(f"{n_unev} 段无法评估")
        summary = "；".join(parts)

    return {
        "format_version": ASSESSMENT_FORMAT_VERSION,
        "verdict": overall,
        "summary": summary,
        "disclaimer": NON_CAUSAL_DISCLAIMER,
        "counts": {"pass": n_pass, "fail": n_fail, "unevaluable": n_unev},
        "anchors": {
            name: (
                None
                if t is None
                else {
                    "t_s": round(float(t), 3),
                    "source": (
                        cur[name]["source"]
                        if name in cur
                        else "anchor_schedule"
                    ),
                }
            )
            for name, t in anchors.items()
        },
        "segments": segments_out,
        "data_basis": {
            "temperature_evidence_window_s": TEMP_EVIDENCE_WINDOW_S,
            "max_gap_fill_s": float(max_gap_fill_s),
            "interpolated_points_count_as_evidence": False,
            "missing_data_is_never_a_pass": True,
        },
        "target_curve": target_curves(canon, anchors),
    }


def target_curves(canon: dict, anchors: dict[str, float | None]) -> dict:
    """Target polylines for chart overlay.

    * ``planned``: the curve as authored, on its planned relative time grid
      (charge=0, durations from the definition, schedule anchors respected) —
      useful as the stable template the lead confirmed;
    * ``mapped``: the same targets aligned to THIS batch's observed anchors,
      split per segment (null between unlocatable segments) so the chart can
      overlay actuals against where the target actually fell.
    """
    planned_pts: list[list[float]] = []
    t_cursor = 0.0
    sched = canon["anchor_schedule"]
    # Advance cursor using scheduled anchor times where given.
    sched_t: dict[str, float] = {"charge": sched.get("charge", 0.0)}
    for seg in canon["segments"]:
        fa = seg["from_anchor"]
        if fa in sched and fa not in sched_t:
            sched_t[fa] = float(sched[fa])
        t0 = sched_t.get(fa, t_cursor)
        t1 = t0 + seg["target_duration_s"]
        sched_t[seg["to_anchor"]] = t1
        t_cursor = t1
        _append_target_run(planned_pts, seg, t0, t1)

    mapped_segments: list[dict] = []
    for seg in canon["segments"]:
        t0 = anchors.get(seg["from_anchor"])
        t1 = anchors.get(seg["to_anchor"])
        if t0 is None or t1 is None:
            mapped_segments.append({"id": seg["id"], "points": None, "band": None})
            continue
        pts: list[list[float]] = []
        _append_target_run(pts, seg, t0, t1)
        band = []
        for t, temp in pts:
            tol = _tol_at(seg, pts, t)
            band.append([t, round(temp - tol, 3), round(temp + tol, 3)])
        mapped_segments.append({"id": seg["id"], "points": pts, "band": band})

    return {
        "planned": {"points": planned_pts},
        "mapped": mapped_segments,
    }


def _append_target_run(
    out: list[list[float]], seg: dict, t0: float, t1: float
) -> None:
    dur = max(t1 - t0, 1e-9)
    # linear interpolation helper for a list of (offset, temp) controls
    ctrls = [(0.0, None)]  # start temp unknown unless provided
    start_temp = seg.get("start_temp_c")
    if start_temp is not None:
        ctrls[0] = (0.0, float(start_temp))
    for pt in seg["shape"]:
        ctrls.append((pt["offset_frac"], float(pt["target_temp_c"])))
    if "end_temp_c" in seg:
        ctrls.append((1.0, float(seg["end_temp_c"])))
    temps = [(o, v) for o, v in ctrls if v is not None]
    if not temps:
        return

    def temp_at(frac: float) -> float:
        for (o0, v0), (o1, v1) in zip(temps, temps[1:]):
            if o0 <= frac <= o1:
                r = 0.0 if o1 == o0 else (frac - o0) / (o1 - o0)
                return v0 + (v1 - v0) * r
        return temps[-1][1]

    # Emit at controls + ends so piecewise-linear overlay is exact.
    fracs = sorted({0.0, 1.0, *(o for o, _ in temps)})
    for f in fracs:
        out.append([round(t0 + f * dur, 3), round(temp_at(f), 3)])


def _tol_at(seg: dict, pts: list[list[float]], t: float) -> float:
    """Tolerance half-width at an emitted overlay point (for the band)."""
    end_tol = seg.get("end_temp_tolerance_c")
    ctrls = [(pt["offset_frac"], float(pt["tolerance_c"])) for pt in seg["shape"]]
    if end_tol is not None:
        ctrls.append((1.0, float(end_tol)))
    if not ctrls:
        # duration-only segment: no temperature band
        return 0.0
    t0, t1 = pts[0][0], pts[-1][0]
    frac = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
    for (o0, v0), (o1, v1) in zip(ctrls, ctrls[1:]):
        if o0 <= frac <= o1:
            r = 0.0 if o1 == o0 else (frac - o0) / (o1 - o0)
            return v0 + (v1 - v0) * r
    return ctrls[-1][1]
