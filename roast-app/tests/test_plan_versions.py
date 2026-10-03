"""Acceptance tests for roast-plan versions: bindings, immutability,
honest unevaluable segments, optimistic version conflicts, re-review flags,
and reproducible exports carrying the plan snapshot."""
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import RoRConfig, build_series
from app.plans import attach_guide, evaluate_plan


DEFN = {
    "segments": [
        {"id": "drying", "from_anchor": "charge", "to_anchor": "turning_point",
         "target_duration_s": 50, "duration_tolerance_s": 25,
         "end_temp_c": 106, "end_temp_tolerance_c": 12},
        {"id": "maillard", "from_anchor": "turning_point",
         "to_anchor": "first_crack_start",
         "target_duration_s": 435, "duration_tolerance_s": 30,
         "end_temp_c": 192, "end_temp_tolerance_c": 10},
        {"id": "development", "from_anchor": "first_crack_start",
         "to_anchor": "drop",
         "target_duration_s": 120, "duration_tolerance_s": 30,
         "end_temp_c": 202, "end_temp_tolerance_c": 8},
    ]
}


def _seed_batches(client):
    a, b = [x["id"] for x in client.post("/api/seed").json()]
    return a, b


def _make_confirmed_plan(client, name="PLAN-ACC", definition=DEFN, by="lead"):
    r = client.post("/api/plans", json={
        "name": name, "created_by": by, "definition": definition})
    assert r.status_code == 201, r.text
    plan = r.json()
    v1 = plan["versions"][0]
    r = client.post(f"/api/plans/{plan['id']}/versions/{v1['id']}/confirm",
                    json={"by": by})
    assert r.status_code == 200, r.text
    return plan["id"], v1["id"]


def _bind_and_assess(client, batch_id, version_id):
    r = client.put(f"/api/batches/{batch_id}/plan-binding",
                   json={"plan_version_id": version_id})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/batches/{batch_id}/plan-assessments", json={})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# ① bind confirmed plan -> anchor-aligned per-segment deviation + reproducible
# ---------------------------------------------------------------------------

def test_bind_confirmed_plan_shows_per_segment_deviation_and_export_reproduces(client):
    a, _ = _seed_batches(client)
    pid, vid = _make_confirmed_plan(client, "PLAN-A1")
    assessment = _bind_and_assess(client, a, vid)

    result = assessment["result"]
    segs = {s["id"]: s for s in result["segments"]}
    # anchor alignment: observed durations come from the batch's actual anchors
    assert segs["drying"]["start_s"] == 0
    assert segs["drying"]["end_s"] == pytest.approx(53.6, abs=1)
    assert segs["development"]["observed_duration_s"] == pytest.approx(120, abs=1)
    # every segment exposes deviation vs tolerance, aligned by anchor
    for sid in ("drying", "maillard", "development"):
        dur = next(c for c in segs[sid]["checks"] if c["kind"] == "duration")
        assert {"observed_s", "target_s", "tolerance_s", "deviation_s",
                "within_tolerance", "status"} <= set(dur)

    # series payload carries the binding + immutable snapshot + assessment
    series = client.get(f"/api/batches/{a}/series").json()
    block = series["plan"]
    assert block["binding"]["plan_version_id"] == vid
    assert block["version_snapshot"]["version_no"] == 1
    assert block["version_snapshot"]["status"] == "confirmed"
    assert block["assessment"]["verdict"] == assessment["verdict"]

    # export carries the plan snapshot and reproduces the same judgement
    ex = client.get(f"/api/batches/{a}/export").json()
    assert ex["export_version"] == 2
    pe = ex["plan_export"]
    snap = pe["version_snapshots"][-1]
    assert snap["definition"]["segments"][0]["target_duration_s"] == 50
    assert snap["content_hash"] == block["version_snapshot"]["content_hash"]
    stored_result = pe["assessments"][-1]["result"]

    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"],
             "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "params": ex["params"],
        "plan_definition": snap["definition"],
    }).json()
    pa = rc["plan_assessment"]
    assert pa["verdict"] == stored_result["verdict"]
    for fresh, stored in zip(pa["segments"], stored_result["segments"]):
        assert fresh["id"] == stored["id"]
        assert fresh["status"] == stored["status"]
        assert fresh["observed_duration_s"] == stored["observed_duration_s"]
    assert pa["target_curve"]["mapped"][0]["points"][0][0] == 0
    # no causal / quality claim is ever produced
    assert "不构成" in pa["disclaimer"] and "成品质量" in pa["disclaimer"]


def test_draft_cannot_be_bound_and_confirmed_version_is_immutable(client):
    a, _ = _seed_batches(client)
    r = client.post("/api/plans", json={"name": "PLAN-IMM", "definition": DEFN})
    plan = r.json()
    v1 = plan["versions"][0]
    # cannot bind a draft
    r = client.put(f"/api/batches/{a}/plan-binding",
                   json={"plan_version_id": v1["id"]})
    assert r.status_code == 422
    # cannot confirm with an invalid definition either
    bad = {"segments": [{"id": "drying", "target_duration_s": -1,
                         "duration_tolerance_s": 5}]}
    r = client.post("/api/plans", json={"name": "PLAN-BAD", "definition": bad})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ② new version adjusts targets: new batches use the new one; old batch/export
# ---------------------------------------------------------------------------

def test_new_version_targets_apply_forward_only_old_binding_and_export_keep_v1(client):
    a, b = _seed_batches(client)
    pid, v1 = _make_confirmed_plan(client, "PLAN-VER")
    ass_old = _bind_and_assess(client, a, v1)
    export_old = client.get(f"/api/batches/{a}/export").json()

    # lead adjusts the drying target (50 -> 90) in a NEW version
    edited = json.loads(json.dumps(DEFN))
    edited["segments"][0]["target_duration_s"] = 90
    r = client.post(f"/api/plans/{pid}/versions",
                    json={"based_on_version_id": v1, "definition": edited})
    assert r.status_code == 201, r.text
    v2draft = r.json()
    assert v2draft["version_no"] == 2 and v2draft["based_on_version_id"] == v1
    r = client.post(f"/api/plans/{pid}/versions/{v2draft['id']}/confirm", json={})
    assert r.status_code == 200
    v2 = v2draft["id"]

    # old confirmed content must be byte-identical (immutable)
    v1row = client.get(f"/api/plans/{pid}/versions/{v1}").json()
    assert v1row["status"] == "retired"
    assert v1row["definition"]["segments"][0]["target_duration_s"] == 50

    # newly bound batch B uses v2
    _bind_and_assess(client, b, v2)
    block_b = client.get(f"/api/batches/{b}/series").json()["plan"]
    assert block_b["version_snapshot"]["version_no"] == 2
    assert block_b["version_snapshot"]["definition"]["segments"][0][
        "target_duration_s"] == 90

    # old batch A still judged against v1, with its original verdict
    block_a = client.get(f"/api/batches/{a}/series").json()["plan"]
    assert block_a["version_snapshot"]["version_no"] == 1
    assert block_a["assessment"]["verdict"] == ass_old["verdict"]
    assert block_a["version_snapshot"]["content_hash"] == \
        export_old["plan_export"]["version_snapshots"][-1]["content_hash"]

    # old export's v1 snapshot is unaffected and still reproduces v1 judgement
    snap = export_old["plan_export"]["version_snapshots"][-1]
    assert snap["definition"]["segments"][0]["target_duration_s"] == 50
    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"],
             "env_temp_c": p["env_temp_c"]}
            for p in export_old["series"]["raw_points"]
        ],
        "events": export_old["events"],
        "params": export_old["params"],
        "plan_definition": snap["definition"],
    }).json()
    assert rc["plan_assessment"]["verdict"] == ass_old["verdict"]

    # rebinding A to v2 is a NEW binding row; old binding retained in history
    r = client.put(f"/api/batches/{a}/plan-binding",
                   json={"plan_version_id": v2})
    assert r.status_code == 200
    hist = client.get(f"/api/batches/{a}/plan-binding/history").json()
    assert len(hist) == 2
    assert hist[0]["superseded"] is True and hist[0]["plan_version_id"] == v1
    assert hist[1]["superseded"] is False and hist[1]["plan_version_id"] == v2


# ---------------------------------------------------------------------------
# ③ missing anchor / long gap -> unevaluable with reason, never a fake pass
# ---------------------------------------------------------------------------

def test_missing_first_crack_marks_segments_unevaluable(client):
    from app import models
    a, _ = _seed_batches(client)
    pid, vid = _make_confirmed_plan(client, "PLAN-FC")
    _bind_and_assess(client, a, vid)

    # delete the first_crack_start EVENT row at the storage layer (simulate
    # a batch where FC was never recorded) then reassess.
    with Session(models.engine) as s:
        fc = s.scalars(
            select(models.Event).where(
                models.Event.batch_id == a,
                models.Event.event_type == "first_crack_start",
            )
        ).all()
        for e in fc:
            s.delete(e)
        s.commit()

    r = client.post(f"/api/batches/{a}/plan-assessments", json={"assessed_by": "lead"})
    assert r.status_code == 201
    segs = {x["id"]: x for x in r.json()["result"]["segments"]}
    for sid in ("maillard", "development"):
        assert segs[sid]["status"] == "unevaluable"
        assert segs[sid]["reason_code"] == "missing_anchor"
        assert "first_crack_start" in segs[sid]["missing_anchors"]
        assert segs[sid]["checks"] == []
        assert "无法" in segs[sid]["reason_zh"]
    # drying does not need FC
    assert segs["drying"]["status"] == "pass"


def test_segment_crossing_long_gap_is_unevaluable_and_interpolation_not_pass(client):
    a, _ = _seed_batches(client)
    pid, vid = _make_confirmed_plan(client, "PLAN-GAP")
    r = client.put(f"/api/batches/{a}/plan-binding",
                   json={"plan_version_id": vid})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/batches/{a}/plan-assessments", json={})
    segs = {x["id"]: x for x in r.json()["result"]["segments"]}
    # maillard (~54 -> 480 s) crosses the 424-480 s wide unfilled gap
    m = segs["maillard"]
    assert m["status"] == "unevaluable"
    assert m["reason_code"] == "crosses_wide_unfilled_gap"
    assert m["blocked_by_gap"]["t_end_s"] >= 470
    assert "插值" in m["reason_zh"] or "缺测" in m["reason_zh"]
    # overall verdict must not claim pass
    verdict = r.json()["result"]["verdict"]
    assert verdict in ("unevaluable_segments", "partial_deviation")

    # an interior check whose ±8s evidence window contains ONLY interpolated
    # points (a 30 s gap bridged by linear fill, which is still not measured)
    # must be unevaluable even though interpolation draws a line through it.
    samples = []
    for t in range(0, 520, 10):
        if t in (150, 160):
            samples.append({"t_s": float(t), "bean_temp_c": None,
                            "env_temp_c": 200.0})
        else:
            # gentle climb so the interpolated value would look "plausible"
            samples.append({"t_s": float(t), "bean_temp_c": 100.0 + 0.2 * t,
                            "env_temp_c": 200.0})
    events = [
        {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual",
         "superseded": False},
        {"id": 2, "event_type": "turning_point", "t_s": 60, "source": "manual",
         "superseded": False},
        {"id": 3, "event_type": "first_crack_start", "t_s": 400,
         "source": "manual", "superseded": False},
        {"id": 4, "event_type": "drop", "t_s": 500, "source": "manual",
         "superseded": False},
    ]
    defn_shape = {
        "segments": [
            {"id": "drying", "target_duration_s": 60, "duration_tolerance_s": 10},
            {"id": "maillard", "target_duration_s": 340, "duration_tolerance_s": 10,
             "shape": [{"offset_frac": 100 / 340, "target_temp_c": 130.0,
                        "tolerance_c": 3.0}]},
            {"id": "development", "target_duration_s": 100,
             "duration_tolerance_s": 10},
        ]
    }
    series2 = build_series(samples, ror_cfg=RoRConfig(), max_gap_fill_s=45)
    # the 150-160 s gap is interpolated (30 s span, within bridge limit)
    bean_gaps = [g for g in series2["missing_segments"] if g["channel"] == "bean"]
    assert {g["status"] for g in bean_gaps} == {"interpolated"}
    out = evaluate_plan(
        defn_shape,
        attach_guide(series2["raw_points"], series2["guide_bean_temp"]),
        events,
        max_gap_fill_s=45,
        missing_segments=series2["missing_segments"],
    )
    m2 = next(s for s in out["segments"] if s["id"] == "maillard")
    shape_check = next(c for c in m2["checks"] if c["kind"] == "shape_temp")
    assert shape_check["status"] == "unevaluable"
    assert shape_check["evidence"]["reason_code"] == "only_interpolated_in_window"
    assert "插值" in shape_check["evidence"]["reason_zh"]
    assert shape_check["deviation_c"] is None


# ---------------------------------------------------------------------------
# ④ two editors, same base version -> explicit 409, first writer wins
# ---------------------------------------------------------------------------

def test_concurrent_editors_on_same_base_get_explicit_conflict(client):
    a, _ = _seed_batches(client)
    pid, v1 = _make_confirmed_plan(client, "PLAN-CONF")
    _bind_and_assess(client, a, v1)

    edited_a = json.loads(json.dumps(DEFN))
    edited_a["segments"][0]["target_duration_s"] = 40
    edited_b = json.loads(json.dumps(DEFN))
    edited_b["segments"][0]["target_duration_s"] = 60

    # editor A arrives first
    ra = client.post(f"/api/plans/{pid}/versions",
                     json={"based_on_version_id": v1, "definition": edited_a,
                           "created_by": "A"})
    assert ra.status_code == 201, ra.text
    v2 = ra.json()

    # editor B submits a different change based on the SAME old v1
    rb = client.post(f"/api/plans/{pid}/versions",
                     json={"based_on_version_id": v1, "definition": edited_b,
                           "created_by": "B"})
    assert rb.status_code == 409
    detail = rb.json()["detail"]
    assert detail["error"] == "version_conflict"
    assert detail["latest_version_id"] == v2["id"]
    # B's content was NOT written anywhere
    plan = client.get(f"/api/plans/{pid}").json()
    nos = [v["version_no"] for v in plan["versions"]]
    assert nos == [1, 2]
    v2row = next(v for v in plan["versions"] if v["version_no"] == 2)
    assert v2row["definition"]["segments"][0]["target_duration_s"] == 40

    # concurrent edits inside the open draft also collide via revision
    r1 = client.patch(f"/api/plans/{pid}/versions/{v2['id']}",
                      json={"definition": edited_a, "expected_revision": 1})
    assert r1.status_code == 200 and r1.json()["revision"] == 2
    r2 = client.patch(f"/api/plans/{pid}/versions/{v2['id']}",
                      json={"definition": edited_b, "expected_revision": 1})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "draft_revision_conflict"
    assert r2.json()["detail"]["current_revision"] == 2


# ---------------------------------------------------------------------------
# ⑤ manual anchor correction -> needs_review; stale judgement still viewable
# ---------------------------------------------------------------------------

def test_anchor_correction_flags_needs_review_but_keeps_history(client):
    a, _ = _seed_batches(client)
    pid, vid = _make_confirmed_plan(client, "PLAN-REV")
    before = _bind_and_assess(client, a, vid)
    assert before["review_state"] == "current"
    before_verdict = before["verdict"]
    before_hash = before["content_hash"]

    # manual correction of the turning point
    r = client.post(f"/api/batches/{a}/events", json={
        "event_type": "turning_point", "t_s": 70.0, "source": "manual",
        "created_by": "tester", "label": "人工回温点"})
    assert r.status_code == 200
    assert r.json()["plan_review"]["flagged"] is True

    cur = client.get(f"/api/batches/{a}/plan-assessment").json()
    assert cur["review_state"] == "needs_review"
    # historical verdict, hash and full basis remain intact and viewable
    assert cur["verdict"] == before_verdict
    assert cur["content_hash"] == before_hash
    assert cur["result"]["basis"]["plan_version_id"] == vid
    assert "turning_point" in cur["needs_review_reason"]

    # series payload surfaces the banner state
    block = client.get(f"/api/batches/{a}/series").json()["plan"]
    assert block["assessment"]["review_state"] == "needs_review"

    # damper (non-anchor) correction does NOT flag plan review
    r = client.post(f"/api/batches/{a}/events", json={
        "event_type": "damper_change", "t_s": 250, "value_num": 55,
        "source": "manual"})
    assert r.json()["plan_review"]["flagged"] is False

    # after re-review/recompute a new current row appears; old row superseded
    after = client.post(f"/api/batches/{a}/plan-assessments",
                        json={"assessed_by": "lead"}).json()
    assert after["review_state"] == "current"
    assert after["id"] != before["id"]
    segs = {s["id"]: s for s in after["result"]["segments"]}
    assert segs["drying"]["end_s"] == pytest.approx(70, abs=0.1)

    history = client.get(f"/api/batches/{a}/plan-assessment/history").json()
    states = [(h["id"], h["review_state"]) for h in history]
    assert (before["id"], "superseded") in states
    assert (after["id"], "current") in states
    # stale basis still readable in history
    stale = next(h for h in history if h["id"] == before["id"])
    assert stale["result"]["segments"][0]["end_s"] != 70


def test_demo_seeder_binds_both_batches(client):
    a, b = _seed_batches(client)
    r = client.post("/api/seed-demo-plan")
    assert r.status_code == 201, r.text
    assert set(r.json()["bound_batch_ids"]) == {a, b}
    for bid in (a, b):
        block = client.get(f"/api/batches/{bid}/series").json()["plan"]
        assert block is not None
        assert block["assessment"] is not None
        assert block["version_snapshot"]["status"] == "confirmed"
    # idempotent
    assert client.post("/api/seed-demo-plan").status_code == 201
