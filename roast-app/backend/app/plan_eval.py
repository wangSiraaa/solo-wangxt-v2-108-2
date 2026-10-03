"""Plan-conformance evaluation: anchor-relative target segments vs measured data.

This module is a *pure function* layer, in the same spirit as
:mod:`app.analysis`: it never touches the database and derives every number
from raw samples + current events + one immutable plan-version document.

Honesty rules implemented here
-------------------------------
* Only **measured** bean-temp samples are evidence.  Interpolated points are
  never a basis for a pass; bins that would only be covered by linear
  interpolation are labelled ``interpolated`` and counted separately.
* A segment that crosses a probe gap longer than ``long_gap_threshold_s`` or
  that lacks enough measured coverage is reported ``unevaluated`` with an
  explicit reason code — the evaluator never fills the hole with a fabricated
  "within tolerance" conclusion.
* Missing boundary anchors (turning point / first crack / drop ...) make every
  segment using them ``unevaluated`` rather than guessed.
* The output describes *observed deviation from the bound plan version only*.
  It is not a causal claim and not a product-quality (cupping) verdict; the
  fixed disclaimer is attached to every result and stored with it.

Target segments are positioned *relative to anchors*: each segment goes from
``from_anchor`` to ``to_anchor``; its target curve is piecewise-linear through
control points expressed as a fraction ``p ∈ [0,1]`` of the realised anchor
span, with absolute target temperatures (°C) and a ±tolerance band.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

# Anchor event types a segment may start/end at.  Kept in sync with the
# append-only event vocabulary in models.py / analysis.py.
PLAN_ANCHORS = (
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
)

SEGMENT_KEYS = ("drying", "maillard", "development", "first_crack", "total")

# Default evaluation basis.  A version may override these via
# content["parameters"]; the chosen values are stored in the result and in the
# version document so an export recomputes identically.
DEFAULT_PARAMETERS = {
    "bin_count": 12,
    "min_measured_bin_fraction": 0.75,
    "max_interpolated_bin_fraction": 0.20,
    # A probe gap wider than this (seconds, measured between the flanking
    # measured samples) is an unbridged break and invalidates a segment.
    "long_gap_threshold_s": 45.0,
    # How far from the anchor time a measured sample may be to be quoted as
    # the anchor's observed temperature.
    "anchor_temp_slop_s": 10.0,
    "min_measured_points": 2,
}

EVIDENCE_POLICY = (
    "仅实测豆温点参与方案达标判定；线性插值段不作为达标依据；"
    "跨越超过 {long_gap_threshold_s:g}s 的未桥接探针缺测段、"
    "或实测覆盖不足的目标段判为“未评估”，不补造达标结论。"
)

DISCLAIMER = (
    "偏差结论仅描述本批次实测曲线与所绑定烘焙方案版本之间的观察性差异；"
    "无对照、无重复、无统计检验，不构成因果结论，"
    "也不构成成品质量（杯测/风味）结论。"
)


class PlanContentError(ValueError):
    """The submitted plan-version document is structurally invalid."""


# ---------------------------------------------------------------------------
# validation / canonicalisation
# ---------------------------------------------------------------------------

def validate_plan_content(content: dict) -> dict:
    """Validate a version document and return a normalised copy.

    Raises :class:`PlanContentError` with a Chinese, operator-readable message
    on the first structural problem.  Normalisation fills default parameters
    and orders nothing that carries meaning (segment order is preserved).
    """
    if not isinstance(content, dict):
        raise PlanContentError("方案文档必须是对象")
    segments = content.get("segments")
    if not isinstance(segments, list) or not segments:
        raise PlanContentError("方案必须包含至少一个目标段 segments[]")

    seen_keys: set[str] = set()
    norm_segments = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise PlanContentError(f"第 {i + 1} 个目标段不是对象")
        where = f"目标段 {seg.get('key') or i + 1}"
        key = seg.get("key")
        if key not in SEGMENT_KEYS:
            raise PlanContentError(f"{where} 的 key 无效，允许：{', '.join(SEGMENT_KEYS)}")
        if key in seen_keys:
            raise PlanContentError(f"目标段 key 重复：{key}")
        seen_keys.add(key)
        fa, ta = seg.get("from_anchor"), seg.get("to_anchor")
        if fa not in PLAN_ANCHORS or ta not in PLAN_ANCHORS:
            raise PlanContentError(f"{where} 的锚点无效，允许：{', '.join(PLAN_ANCHORS)}")
        if fa == ta:
            raise PlanContentError(f"{where} 的起止锚点不能相同")
        points = seg.get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise PlanContentError(f"{where} 需要至少 2 个控制点 points[]")
        norm_points = []
        for j, pt in enumerate(points):
            if not isinstance(pt, dict) or "p" not in pt or "temp_c" not in pt:
                raise PlanContentError(f"{where} 控制点 {j + 1} 需要 p 与 temp_c")
            p = float(pt["p"])
            temp = float(pt["temp_c"])
            if not (0.0 <= p <= 1.0) or not np.isfinite(temp):
                raise PlanContentError(f"{where} 控制点 {j + 1} 的 p 必须在 [0,1]、温度有限")
            norm_points.append({"p": p, "temp_c": temp})
        ps = [p["p"] for p in norm_points]
        if ps != sorted(ps) or ps[0] != 0.0 or ps[-1] != 1.0:
            raise PlanContentError(f"{where} 控制点必须按 p 递增且覆盖 0 到 1")
        tol = seg.get("temp_tolerance_c")
        if tol is None or float(tol) <= 0:
            raise PlanContentError(f"{where} 需要正数容差 temp_tolerance_c")
        norm_seg = {
            "key": key,
            "label": str(seg.get("label") or key),
            "from_anchor": fa,
            "to_anchor": ta,
            "points": norm_points,
            "temp_tolerance_c": float(tol),
        }
        if seg.get("target_duration_s") is not None:
            dur = float(seg["target_duration_s"])
            dtol = seg.get("duration_tolerance_s")
            if dur <= 0 or dtol is None or float(dtol) < 0:
                raise PlanContentError(f"{where} 的目标时长/时长容差非法")
            norm_seg["target_duration_s"] = dur
            norm_seg["duration_tolerance_s"] = float(dtol)
        norm_segments.append(norm_seg)

    parameters = dict(DEFAULT_PARAMETERS)
    if isinstance(content.get("parameters"), dict):
        for k, v in content["parameters"].items():
            if k in DEFAULT_PARAMETERS:
                parameters[k] = type(DEFAULT_PARAMETERS[k])(v)
    if parameters["bin_count"] < 2:
        raise PlanContentError("parameters.bin_count 至少为 2")

    return {
        "segments": norm_segments,
        "parameters": parameters,
        "note": str(content.get("note", "")),
    }


def canonical_content_json(content: dict) -> bytes:
    """Deterministic JSON bytes used for content fingerprints / snapshots."""
    return json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def content_sha256(content: dict) -> str:
    return hashlib.sha256(canonical_content_json(content)).hexdigest()


def anchor_fingerprint(events: list[dict]) -> str:
    """Hash the *current* anchor events (type/id/t_s).

    A manual event correction changes this fingerprint, which is how a stored
    evaluation is later detected as stale (needs re-review).
    """
    cur: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (e["t_s"], e["id"])):
        if not e.get("superseded") and e.get("event_type") in PLAN_ANCHORS:
            cur[e["event_type"]] = e
    parts = [f"{k}:{e['id']}:{float(e['t_s']):.3f}" for k, e in sorted(cur.items())]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# gap helpers (measured samples only — same missing-run notion as analysis)
# ---------------------------------------------------------------------------

def _missing_runs(t: np.ndarray, temp: np.ndarray) -> list[dict]:
    """Internal runs of NULL samples, with neighbour-to-neighbour span."""
    measured = ~np.isnan(temp)
    idx = np.arange(len(t))
    miss = idx[~measured]
    runs: list[dict] = []
    if len(miss) == 0:
        return runs
    start = int(miss[0])
    prev = start
    for j in miss[1:]:
        j = int(j)
        if j != prev + 1:
            runs.append((start, prev))
            start = j
        prev = j
    runs.append((start, prev))

    out = []
    for a, b in runs:
        if a == 0 or b == len(t) - 1:
            continue  # edge dropout: no interpolation possible either
        out.append(
            {
                "t_start_s": float(t[a]),
                "t_end_s": float(t[b]),
                "neighbour_span_s": float(t[b + 1] - t[a - 1]),
            }
        )
    return out


def _r3(v) -> float | None:
    if v is None or not np.isfinite(v):
        return None
    return round(float(v), 3)


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

def evaluate_plan(
    samples: list[dict],
    events: list[dict],
    content: dict,
) -> dict:
    """Evaluate one batch against one (validated) plan-version document.

    Returns a structured, JSON-serialisable result.  Segments are aligned on
    the realised anchor times from the *current* (non-superseded) events.

    Segment / overall verdicts:
      ``within_tolerance`` | ``outside_tolerance`` | ``unevaluated``
    Overall ``incomplete`` if any segment is unevaluated — the rest of the
    segments are still evaluated individually and shown with their evidence.
    """
    norm = validate_plan_content(content)
    params = norm["parameters"]
    long_gap = float(params["long_gap_threshold_s"])

    samples = sorted(samples, key=lambda s: s["t_s"])
    t = np.array([s["t_s"] for s in samples], dtype=float)
    bean = np.array(
        [np.nan if s["bean_temp_c"] is None else s["bean_temp_c"] for s in samples],
        dtype=float,
    )
    measured_mask = ~np.isnan(bean)
    t_m = t[measured_mask]
    y_m = bean[measured_mask]
    gaps = _missing_runs(t, bean)
    long_gaps = [g for g in gaps if g["neighbour_span_s"] > long_gap]
    short_gaps = [g for g in gaps if g["neighbour_span_s"] <= long_gap]

    cur: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (e["t_s"], e["id"])):
        if not e.get("superseded") and e.get("event_type") in PLAN_ANCHORS:
            cur[e["event_type"]] = e

    anchors_out: dict[str, dict] = {}
    for kind in PLAN_ANCHORS:
        e = cur.get(kind)
        entry = {
            "present": e is not None,
            "t_s": _r3(e["t_s"]) if e else None,
            "source": e["source"] if e else None,
            "event_id": e["id"] if e else None,
            "temp_c": None,
            "temp_sample_t_s": None,
            "temp_delta_s": None,
        }
        if e is not None and len(t_m):
            j = int(np.argmin(np.abs(t_m - float(e["t_s"]))))
            delta = float(abs(t_m[j] - float(e["t_s"])))
            if delta <= float(params["anchor_temp_slop_s"]):
                entry["temp_c"] = _r3(y_m[j])
                entry["temp_sample_t_s"] = _r3(t_m[j])
                entry["temp_delta_s"] = _r3(delta)
        anchors_out[kind] = entry

    segment_results = []
    for seg in norm["segments"]:
        segment_results.append(
            _evaluate_segment(seg, cur, t_m, y_m, long_gaps, short_gaps, params)
        )

    statuses = {s["verdict"] for s in segment_results}
    if statuses <= {"within_tolerance"}:
        overall = "within_tolerance"
    elif "unevaluated" in statuses:
        overall = "incomplete"
    else:
        overall = "outside_tolerance"

    return {
        "schema_version": 1,
        "overall_verdict": overall,
        "anchors": anchors_out,
        "segments": segment_results,
        "parameters": params,
        "missing_segments": [
            {
                "t_start_s": _r3(g["t_start_s"]),
                "t_end_s": _r3(g["t_end_s"]),
                "neighbour_span_s": _r3(g["neighbour_span_s"]),
                "kind": "long_unfilled" if g["neighbour_span_s"] > long_gap
                else "short_interpolated",
            }
            for g in gaps
        ],
        "evidence_policy": EVIDENCE_POLICY.format(**params),
        "disclaimer": DISCLAIMER,
    }


def _gap_overlap(g: dict, lo: float, hi: float) -> float:
    """Overlap length between a missing run (flanking-sample span) and [lo,hi]."""
    a, b = g["t_start_s"], g["t_end_s"]
    return max(0.0, min(b, hi) - max(a, lo))


def _evaluate_segment(
    seg: dict,
    cur: dict[str, dict],
    t_m: np.ndarray,
    y_m: np.ndarray,
    long_gaps: list[dict],
    short_gaps: list[dict],
    params: dict,
) -> dict:
    """Evaluate one target segment. See module docstring for the rules."""
    fa, ta = seg["from_anchor"], seg["to_anchor"]
    base = {
        "key": seg["key"],
        "label": seg["label"],
        "from_anchor": fa,
        "to_anchor": ta,
        "temp_tolerance_c": _r3(seg["temp_tolerance_c"]),
        "target_duration_s": seg.get("target_duration_s"),
        "duration_tolerance_s": seg.get("duration_tolerance_s"),
    }

    missing = [k for k in (fa, ta) if k not in cur]
    if missing:
        return {
            **base,
            "verdict": "unevaluated",
            "reasons": [
                {
                    "code": "missing_anchor",
                    "message": f"缺少锚点事件：{'、'.join(missing)}；目标段无法定位，不做达标判断。",
                }
            ],
            "from_t_s": None,
            "to_t_s": None,
            "duration_s": None,
            "duration_verdict": None,
            "duration_deviation_s": None,
            "bins": [],
            "n_measured_bins": 0,
            "n_interpolated_bins": 0,
            "n_long_gap_bins": 0,
            "max_abs_deviation_c": None,
            "curve_verdict": None,
        }

    t0, t1 = float(cur[fa]["t_s"]), float(cur[ta]["t_s"])
    span = t1 - t0
    if span <= 0:
        return {
            **base,
            "verdict": "unevaluated",
            "reasons": [
                {
                    "code": "anchor_order",
                    "message": f"锚点 {fa} 不早于 {ta}，目标段时长非正，无法判断。",
                }
            ],
            "from_t_s": _r3(t0),
            "to_t_s": _r3(t1),
            "duration_s": _r3(span),
            "duration_verdict": None,
            "duration_deviation_s": None,
            "bins": [],
            "n_measured_bins": 0,
            "n_interpolated_bins": 0,
            "n_long_gap_bins": 0,
            "max_abs_deviation_c": None,
            "curve_verdict": None,
        }

    n_bins = int(params["bin_count"])
    edges = np.linspace(t0, t1, n_bins + 1)
    ctrl_p = np.array([p["p"] for p in seg["points"]], dtype=float)
    ctrl_t = np.array([p["temp_c"] for p in seg["points"]], dtype=float)

    bins = []
    n_measured = n_interp = n_long = 0
    deviations: list[float] = []
    measured_points_total = 0
    for k in range(n_bins):
        lo, hi = float(edges[k]), float(edges[k + 1])
        mid = (lo + hi) / 2.0
        p = (mid - t0) / span
        target = float(np.interp(p, ctrl_p, ctrl_t))
        # half-open bins, closed on the final one
        if k == n_bins - 1:
            inside = (t_m >= lo) & (t_m <= hi)
        else:
            inside = (t_m >= lo) & (t_m < hi)
        if inside.any():
            ys = y_m[inside]
            measured_points_total += int(inside.sum())
            obs = float(np.mean(ys))
            dev = obs - target
            within = abs(dev) <= float(seg["temp_tolerance_c"]) + 1e-9
            deviations.append(abs(dev))
            bins.append(
                {
                    "index": k,
                    "kind": "measured",
                    "t_s": _r3(mid),
                    "p": _r3(p),
                    "observed_temp_c": _r3(obs),
                    "target_temp_c": _r3(target),
                    "deviation_c": _r3(dev),
                    "within_tolerance": bool(within),
                    "n_measured_points": int(inside.sum()),
                }
            )
            n_measured += 1
            continue

        hit_long = next(
            (g for g in long_gaps if _gap_overlap(g, lo, hi) > 0), None
        )
        if hit_long is not None:
            kind = "long_gap"
            n_long += 1
        else:
            # No measured sample inside the bin.  If a measured probe exists on
            # both sides this bin could only be drawn via the query-time linear
            # fill — it is interpolation territory and never evidence; a gap
            # beyond the last measured sample is simply no_data.
            between = (t_m < hi).sum()
            if 0 < between < len(t_m) or any(
                _gap_overlap(g, lo, hi) > 0 for g in short_gaps
            ):
                kind = "interpolated"
                n_interp += 1
            else:
                kind = "no_data"
        bins.append(
            {
                "index": k,
                "kind": kind,
                "t_s": _r3(mid),
                "p": _r3(p),
                "observed_temp_c": None,
                "target_temp_c": _r3(target),
                "deviation_c": None,
                "within_tolerance": None,
                "n_measured_points": 0,
            }
        )

    reasons: list[dict] = []
    # A long unbridged probe break touching the *interior* of the target
    # segment is decisive: the segment straddles a region where the bean
    # temperature was never observed, so the missing stretch can never be
    # "within tolerance".  A boundary touching the gap edge (overlap 0) is
    # allowed — the anchors themselves are still explicit events.
    long_overlaps = [
        {
            "neighbour_span_s": _r3(g["neighbour_span_s"]),
            "overlap_s": _r3(_gap_overlap(g, t0, t1)),
        }
        for g in long_gaps
        if _gap_overlap(g, t0, t1) > 1e-9
    ]
    if long_overlaps:
        total_overlap = sum(d["overlap_s"] for d in long_overlaps)
        reasons.append(
            {
                "code": "crosses_long_gap",
                "message": (
                    f"目标段内部有 {total_overlap:g}s 与未桥接探针缺测重叠"
                    f"（缺口相邻实测跨度最大 "
                    f"{max(d['neighbour_span_s'] for d in long_overlaps):g}s，"
                    f"超过 {params['long_gap_threshold_s']:g}s 桥接上限）；"
                    "相邻插值不能作为通过依据，该段未评估。"
                ),
                "gaps": long_overlaps,
            }
        )

    measured_fraction = n_measured / n_bins
    interp_fraction = n_interp / n_bins
    if measured_points_total < int(params["min_measured_points"]):
        reasons.append(
            {
                "code": "insufficient_measured_coverage",
                "message": (
                    f"段内实测豆温点仅 {measured_points_total} 个"
                    f"（至少需要 {params['min_measured_points']} 个），证据不足，未评估。"
                ),
            }
        )
    elif measured_fraction < float(params["min_measured_bin_fraction"]):
        reasons.append(
            {
                "code": "insufficient_measured_coverage",
                "message": (
                    f"段内实测覆盖 {measured_fraction:.0%}"
                    f"（低于 {float(params['min_measured_bin_fraction']):.0%} 下限），"
                    "缺测/插值占比过高，未评估。"
                ),
            }
        )
    if interp_fraction > float(params["max_interpolated_bin_fraction"]):
        reasons.append(
            {
                "code": "too_much_interpolation",
                "message": (
                    f"段内仅能靠线性插值覆盖的时间格占 {interp_fraction:.0%}"
                    f"（超过 {float(params['max_interpolated_bin_fraction']):.0%} 上限），"
                    "插值不作为达标依据，未评估。"
                ),
            }
        )

    # Duration verdict vs the anchor-relative target duration.
    duration_verdict = None
    duration_dev = None
    target_dur = seg.get("target_duration_s")
    if target_dur is not None:
        duration_dev = span - target_dur
        duration_verdict = (
            "within_tolerance"
            if abs(duration_dev) <= float(seg["duration_tolerance_s"]) + 1e-9
            else "outside_tolerance"
        )

    # Target polyline mapped onto absolute time for chart overlay.
    target_line = [
        {"t_s": _r3(t0 + p * span), "p": _r3(p), "temp_c": _r3(v)}
        for p, v in zip(ctrl_p, ctrl_t)
    ]
    band = [
        {
            "t_s": pt["t_s"],
            "upper_c": _r3(pt["temp_c"] + float(seg["temp_tolerance_c"])),
            "lower_c": _r3(pt["temp_c"] - float(seg["temp_tolerance_c"])),
        }
        for pt in target_line
    ]

    if reasons:
        curve_verdict = None
        verdict = "unevaluated"
    else:
        curve_verdict = (
            "within_tolerance"
            if deviations and all(
                abs(b["deviation_c"]) <= float(seg["temp_tolerance_c"]) + 1e-9
                for b in bins
                if b["kind"] == "measured"
            )
            else "outside_tolerance"
        )
        if curve_verdict == "within_tolerance" and (
            duration_verdict is None or duration_verdict == "within_tolerance"
        ):
            verdict = "within_tolerance"
        else:
            verdict = "outside_tolerance"

    return {
        **base,
        "verdict": verdict,
        "curve_verdict": curve_verdict,
        "reasons": reasons,
        "from_t_s": _r3(t0),
        "to_t_s": _r3(t1),
        "duration_s": _r3(span),
        "duration_verdict": duration_verdict,
        "duration_deviation_s": _r3(duration_dev),
        "bins": bins,
        "n_measured_bins": n_measured,
        "n_interpolated_bins": n_interp,
        "n_long_gap_bins": n_long,
        "max_abs_deviation_c": _r3(max(deviations)) if deviations else None,
        "target_line": target_line,
        "tolerance_band": band,
    }
