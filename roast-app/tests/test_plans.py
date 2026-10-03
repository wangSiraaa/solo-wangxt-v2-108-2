"""Acceptance tests for roast-plan versions, bindings and deviation judgments.

Covers the five acceptance items from the spec:
  ① bound confirmed plan -> per-segment aligned deviations/tolerances, export
    reproduces the same judgment;
  ② new plan version -> new bindings use it; old batch + old export keep v1;
  ③ missing first crack / long dropout segment -> 未评估 with a reason, never
    a pass inferred from adjacent interpolation;
  ④ two editors from the same base version -> the late submit gets 409 and
    does not overwrite the early one;
  ⑤ manual event correction -> stored plan judgment flagged needs_review while
    the historical judgment and its evidence remain readable.
"""
import copy
import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(fresh_db):
    # Every plan test starts with an empty database; binding/history counts
    # then hold regardless of execution order.
    yield


def _seed_batch(client, index=0):
    r = client.post("/api/seed")
    assert r.status_code == 200
    return r.json()[index]["id"]


def _make_plan(client, content=None, *, name="方案-测试", confirm=True):
    from app.default_plan import DEFAULT_PLAN_CONTENT

    content = copy.deepcopy(content or DEFAULT_PLAN_CONTENT)
    r = client.post(
        "/api/plans", json={"name": name, "content": content}
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    if confirm:
        r = client.post(f"/api/plans/{plan['id']}/versions/1/confirm", json={})
        assert r.status_code == 200, r.text
        plan = client.get(f"/api/plans/{plan['id']}").json()
    return plan


def _bind(client, batch_id, version_id):
    r = client.post(
        f"/api/batches/{batch_id}/bind-plan", json={"plan_version_id": version_id}
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# ① binding -> aligned per-segment deviations + reproducible export
# ---------------------------------------------------------------------------

def test_bind_confirmed_version_aligns_segments_and_export_reproduces(client):
    aid = _seed_batch(client)
    plan = _make_plan(client, name="①完整绑定")
    v1_id = plan["versions"][0]["id"]

    # drafts cannot be bound — only confirmed immutable versions
    draft = _make_plan(client, name="①草稿不可绑定", confirm=False)
    r = client.post(
        f"/api/batches/{aid}/bind-plan",
        json={"plan_version_id": draft["versions"][0]["id"]},
    )
    assert r.status_code == 409

    binding = _bind(client, aid, v1_id)
    result = binding["evaluations"][0]["result"]

    # every segment is anchor-aligned and carries target + tolerance
    keys = [s["key"] for s in result["segments"]]
    assert keys == ["drying", "maillard", "development"]
    for seg in result["segments"]:
        assert seg["from_t_s"] is not None or seg["verdict"] == "unevaluated"
        assert seg["temp_tolerance_c"] > 0
        if seg["verdict"] != "unevaluated":
            assert seg["tolerance_band"] and seg["target_line"]
            assert seg["curve_verdict"] in ("within_tolerance", "outside_tolerance")
            assert all(
                b["kind"] == "measured" and b["within_tolerance"] in (True, False)
                for b in seg["bins"]
                if b["kind"] == "measured"
            )

    # the batch series payload surfaces the stored judgment + version snapshot
    d = client.get(f"/api/batches/{aid}/series").json()
    assert d["plan"]["binding"]["plan_version_id"] == v1_id
    assert d["plan"]["stale"] is False
    assert d["plan"]["current_evaluation"]["review_state"] == "current"

    # export carries the plan snapshot + stored conclusion; recompute from the
    # frozen snapshot reproduces the SAME judgment after ...
    ex = client.get(f"/api/batches/{aid}/export").json()
    assert ex["plan_snapshot"]["content_sha256"] == plan["versions"][0]["content_sha256"]
    assert ex["plan_evaluation"]["result"] == result
    rc = client.post(
        "/api/recompute",
        json={
            "samples": [
                {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
                for p in ex["series"]["raw_points"]
            ],
            "events": ex["events"],
            "params": ex["params"],
            "plan_snapshot": ex["plan_snapshot"],
        },
    ).json()
    assert rc["recompute_plan_evaluation"]["result"] == result
    assert (
        rc["recompute_plan_evaluation"]["content_sha256"]
        == ex["plan_snapshot"]["content_sha256"]
    )
    # observational, never causal/quality language
    assert "不构成因果结论" in result["disclaimer"]
    assert "成品质量" in result["disclaimer"]


# ---------------------------------------------------------------------------
# ② new version -> new bindings use it; old batch/export keep the old version
# ---------------------------------------------------------------------------

def test_new_version_does_not_change_old_binding_or_old_export(client):
    aid = _seed_batch(client)
    bid = _seed_batch(client, index=1)
    plan = _make_plan(client, name="②版本演进")
    v1 = plan["versions"][0]

    binding_a = _bind(client, aid, v1["id"])
    assert binding_a["plan_version_id"] == v1["id"]
    export_a_v1 = client.get(f"/api/batches/{aid}/export").json()
    assert export_a_v1["plan_snapshot"]["version_no"] == 1

    # adjust the development target -> new immutable draft v2 based on v1
    content = copy.deepcopy(v1["content"])
    dev = next(s for s in content["segments"] if s["key"] == "development")
    dev["temp_tolerance_c"] = 0.5  # deliberately much tighter
    r = client.post(
        f"/api/plans/{plan['id']}/versions",
        json={"base_version_no": 1, "content": content, "change_note": "收紧发展期容差"},
    )
    assert r.status_code == 200
    v2 = r.json()
    assert v2["version_no"] == 2 and v2["status"] == "draft"
    assert v2["base_version_no"] == 1
    # v1 row content is untouched
    old = client.get(f"/api/plans/{plan['id']}").json()["versions"]
    assert old[0]["content_sha256"] == v1["content_sha256"]

    # draft v2 not bindable until confirmed; confirming retires v1
    r = client.post(
        f"/api/batches/{bid}/bind-plan", json={"plan_version_id": v2["id"]}
    )
    assert r.status_code == 409
    r = client.post(f"/api/plans/{plan['id']}/versions/2/confirm", json={})
    assert r.status_code == 200
    versions = client.get(f"/api/plans/{plan['id']}").json()["versions"]
    assert versions[0]["status"] == "retired"
    assert versions[1]["status"] == "confirmed"

    # new binding uses v2; old batch A still points at retired v1
    binding_b = _bind(client, bid, v2["id"])
    assert binding_b["plan_version_id"] == v2["id"]
    d_a = client.get(f"/api/batches/{aid}/series").json()
    d_b = client.get(f"/api/batches/{bid}/series").json()
    assert d_a["plan"]["binding"]["plan_version_id"] == v1["id"]
    assert d_b["plan"]["binding"]["plan_version_id"] == v2["id"]

    # old export re-downloaded today still carries v1 and reproduces v1 verdict
    export_a_now = client.get(f"/api/batches/{aid}/export").json()
    assert export_a_now["plan_snapshot"]["version_no"] == 1
    assert export_a_now["plan_snapshot"]["content"] == v1["content"]
    rc = client.post(
        "/api/recompute",
        json={
            "samples": [
                {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
                for p in export_a_now["series"]["raw_points"]
            ],
            "events": export_a_now["events"],
            "params": export_a_now["params"],
            "plan_snapshot": export_a_now["plan_snapshot"],
        },
    ).json()
    assert (
        rc["recompute_plan_evaluation"]["result"]
        == export_a_now["plan_evaluation"]["result"]
    )
    # B under tight v2 deviates in development where A passed under v1
    res_b = binding_b["evaluations"][0]["result"]
    dev_b = next(s for s in res_b["segments"] if s["key"] == "development")
    assert dev_b["verdict"] == "outside_tolerance"


# ---------------------------------------------------------------------------
# ③ missing anchor / long dropout -> 未评估 + reason, interpolation not a pass
# ---------------------------------------------------------------------------

def test_missing_first_crack_segment_unevaluated_with_reason(client):
    # genuinely missing anchor: a manually entered batch whose event list
    # simply has no first-crack events (the API never fabricates anchors).
    samples = [
        {"t_s": t, "bean_temp_c": 100.0 + 0.2 * t, "env_temp_c": 190.0}
        for t in range(0, 601, 10)
    ]
    r = client.post(
        "/api/batches",
        json={
            "name": "BATCH-NO-FC",
            "samples": samples,
            "events": [
                {"event_type": "turning_point", "t_s": 60, "source": "manual"},
                {"event_type": "drop", "t_s": 600, "source": "manual"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    no_fc_id = r.json()["id"]

    plan = _make_plan(client, name="③无一爆")
    res = _bind(client, no_fc_id, plan["versions"][0]["id"])["evaluations"][0]["result"]
    by_key = {seg["key"]: seg for seg in res["segments"]}
    for key in ("maillard", "development"):
        seg = by_key[key]
        assert seg["verdict"] == "unevaluated"
        codes = {r["code"] for r in seg["reasons"]}
        assert "missing_anchor" in codes
        assert seg["bins"] == []
        assert seg["max_abs_deviation_c"] is None
        assert seg["curve_verdict"] is None
    # drying (turning point present; charge anchor implicit via t=0? charge
    # event is also absent) -> drying is unevaluated too; nothing is guessed
    assert by_key["drying"]["verdict"] == "unevaluated"
    assert res["overall_verdict"] == "incomplete"


def test_long_gap_segment_unevaluated_and_interpolation_not_evidence(client):
    aid = _seed_batch(client)
    plan = _make_plan(client, name="③长断档")
    res = _bind(client, aid, plan["versions"][0]["id"])["evaluations"][0]["result"]
    maillard = next(s for s in res["segments"] if s["key"] == "maillard")

    # seeded maillard straddles the 56 s dropout -> unevaluated with reason
    assert maillard["verdict"] == "unevaluated"
    codes = {r["code"] for r in maillard["reasons"]}
    assert "crosses_long_gap" in codes
    assert maillard["curve_verdict"] is None
    # the bin over the gap is explicitly long_gap, never counted as measured
    kinds = [b["kind"] for b in maillard["bins"]]
    assert "long_gap" in kinds
    assert all(
        b["within_tolerance"] is None for b in maillard["bins"] if b["kind"] != "measured"
    )
    # even though measured points on both sides are close-ish, they do not
    # combine into a pass
    assert maillard["max_abs_deviation_c"] is not None  # measured deviations shown ...
    # ... but the verdict stays unevaluated
    assert maillard["verdict"] == "unevaluated"


def test_interpolation_only_bins_never_count_as_measured(client):
    # construct samples where the only coverage in one region is a short gap
    # that WOULD be interpolated for display; plan evaluation must label those
    # bins 'interpolated' rather than pass them.
    from app import plan_eval

    samples = [
        {"t_s": 0, "bean_temp_c": 100.0},
        {"t_s": 10, "bean_temp_c": 102.0},
        {"t_s": 20, "bean_temp_c": None},   # short dropout -> interpolated only
        {"t_s": 30, "bean_temp_c": None},
        {"t_s": 40, "bean_temp_c": 108.0},
        {"t_s": 50, "bean_temp_c": 110.0},
    ]
    events = [
        {"id": 1, "event_type": "turning_point", "t_s": 0, "source": "manual",
         "superseded": False},
        {"id": 2, "event_type": "first_crack_start", "t_s": 50, "source": "manual",
         "superseded": False},
    ]
    content = {
        "segments": [
            {
                "key": "maillard", "label": "m",
                "from_anchor": "turning_point", "to_anchor": "first_crack_start",
                "points": [{"p": 0, "temp_c": 100}, {"p": 1, "temp_c": 110}],
                "temp_tolerance_c": 0.5,
            }
        ],
        "parameters": {"bin_count": 5, "min_measured_bin_fraction": 0.9,
                       "max_interpolated_bin_fraction": 0.0},
    }
    res = plan_eval.evaluate_plan(samples, events, content)
    seg = res["segments"][0]
    kinds = [b["kind"] for b in seg["bins"]]
    assert kinds.count("interpolated") >= 1
    assert seg["verdict"] == "unevaluated"
    assert {r["code"] for r in seg["reasons"]} & {
        "too_much_interpolation",
        "insufficient_measured_coverage",
    }


# ---------------------------------------------------------------------------
# ④ optimistic concurrency: late submit gets an explicit conflict
# ---------------------------------------------------------------------------

def test_two_editors_same_base_late_submit_conflicts(client):
    plan = _make_plan(client, name="④并发冲突", confirm=False)
    content = plan["versions"][0]["content"]
    edit1 = copy.deepcopy(content)
    edit2 = copy.deepcopy(content)
    edit1["note"] = "编辑端甲：备注 A"
    edit2["note"] = "编辑端乙：备注 B"

    # both editors POST based on v1; early one wins as v2
    r1 = client.post(
        f"/api/plans/{plan['id']}/versions",
        json={"base_version_no": 1, "content": edit1, "change_note": "甲"},
    )
    assert r1.status_code == 200
    assert r1.json()["version_no"] == 2

    r2 = client.post(
        f"/api/plans/{plan['id']}/versions",
        json={"base_version_no": 1, "content": edit2, "change_note": "乙"},
    )
    assert r2.status_code == 409
    assert "版本冲突" in r2.json()["detail"]

    # the early version is intact; the late content did not overwrite it
    versions = client.get(f"/api/plans/{plan['id']}").json()["versions"]
    assert len(versions) == 2
    assert versions[1]["content"]["note"] == "编辑端甲：备注 A"

    # editor 乙 rebases on v2 and their change becomes v3
    r3 = client.post(
        f"/api/plans/{plan['id']}/versions",
        json={"base_version_no": 2, "content": edit2, "change_note": "乙 rebase"},
    )
    assert r3.status_code == 200
    assert r3.json()["version_no"] == 3


def test_version_content_is_immutable_after_create(client):
    plan = _make_plan(client, name="④不可变")
    vid = plan["versions"][0]["id"]
    before = client.get(f"/api/plans/{plan['id']}").json()["versions"][0]
    # there is no edit endpoint; attempts to re-PUT content are not routed.
    r = client.put(f"/api/plans/{plan['id']}/versions/1", json={"content": {}})
    assert r.status_code in (404, 405)
    after = client.get(f"/api/plans/{plan['id']}").json()["versions"][0]
    assert after["content_sha256"] == before["content_sha256"]
    assert after["id"] == vid


def test_lifecycle_transitions_are_validated(client):
    plan = _make_plan(client, name="④生命周期")
    # double confirm retired v1 fails
    r = client.post(f"/api/plans/{plan['id']}/versions/1/confirm", json={})
    assert r.status_code == 409
    # create + confirm v2
    content = plan["versions"][0]["content"]
    client.post(
        f"/api/plans/{plan['id']}/versions",
        json={"base_version_no": 1, "content": content, "change_note": "v2"},
    )
    # cannot confirm v1 again (not HEAD)
    r = client.post(f"/api/plans/{plan['id']}/versions/1/confirm", json={})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# ⑤ manual correction flags stored judgment needs_review; history retained
# ---------------------------------------------------------------------------

def test_event_correction_flags_needs_review_but_keeps_history(client):
    aid = _seed_batch(client)
    plan = _make_plan(client, name="⑤修正重审")
    binding = _bind(client, aid, plan["versions"][0]["id"])
    original = binding["evaluations"][0]
    original_fp = original["anchor_fingerprint"]

    # manual correction of turning point supersedes the old event ...
    r = client.post(
        f"/api/batches/{aid}/events",
        json={"event_type": "turning_point", "t_s": 95.0, "source": "manual",
              "created_by": "reviewer", "label": "人工回温点修正"},
    )
    assert r.status_code == 200

    d = client.get(f"/api/batches/{aid}/series").json()
    assert d["plan"]["stale"] is True
    assert "需要重新审阅" in d["plan"]["stale_reason"]
    cur = d["plan"]["current_evaluation"]
    assert cur["id"] == original["id"]  # same stored row ...
    assert cur["review_state"] == "needs_review"  # ... flagged, not rewritten
    assert cur["result"] == original["result"]  # historical basis intact

    # damper changes do not affect anchor-based judgments
    # (correction above already changed the fingerprint)
    rr = client.post(f"/api/batches/{aid}/plan-review", json={"note": "已复核"}).json()
    assert rr["review_state"] == "current"
    assert rr["anchor_fingerprint"] != original_fp
    # new judgment reflects the moved turning point
    assert rr["result"]["anchors"]["turning_point"]["t_s"] == 95.0

    # binding history contains BOTH: obsolete original + fresh current
    history = client.get(f"/api/batches/{aid}/bindings").json()
    assert len(history) == 1
    states = [e["review_state"] for e in history[0]["evaluations"]]
    assert states == ["obsolete", "current"]
    old_row = history[0]["evaluations"][0]
    assert old_row["result"]["anchors"]["turning_point"]["t_s"] != 95.0
    assert old_row["content_sha256"] == plan["versions"][0]["content_sha256"]


def test_rebinding_keeps_old_binding_and_its_judgment(client):
    aid = _seed_batch(client)
    p1 = _make_plan(client, name="⑤重绑A")
    p2 = _make_plan(client, name="⑤重绑B")
    b1 = _bind(client, aid, p1["versions"][0]["id"])
    b2 = _bind(client, aid, p2["versions"][0]["id"])
    assert b2["plan_version_id"] != b1["plan_version_id"]

    history = client.get(f"/api/batches/{aid}/bindings").json()
    assert len(history) == 2
    assert history[0]["superseded"] is True
    assert history[0]["superseded_by_id"] == history[1]["id"]
    assert history[0]["evaluations"][0]["review_state"] == "obsolete"
    assert history[1]["superseded"] is False
    assert history[1]["evaluations"][0]["review_state"] == "current"
    # old binding still carries its plan version snapshot + stored result
    assert history[0]["plan_version"]["id"] == p1["versions"][0]["id"]
    assert history[0]["evaluations"][0]["result"]["segments"]


def test_damper_correction_does_not_invalidate_plan_judgment(client):
    aid = _seed_batch(client)
    plan = _make_plan(client, name="⑤风门不重审")
    _bind(client, aid, plan["versions"][0]["id"])
    r = client.post(
        f"/api/batches/{aid}/events",
        json={"event_type": "damper_change", "t_s": 250, "value_num": 30,
              "source": "manual"},
    )
    assert r.status_code == 200
    d = client.get(f"/api/batches/{aid}/series").json()
    assert d["plan"]["stale"] is False
    assert d["plan"]["current_evaluation"]["review_state"] == "current"


def test_invalid_plan_content_rejected_with_reason(client):
    bad_cases = [
        {},  # no segments
        {"segments": [{"key": "drying", "from_anchor": "charge",
                       "to_anchor": "drop", "points": [{"p": 0, "temp_c": 100}],
                       "temp_tolerance_c": 2}]},  # one point only, missing end
        {"segments": [{"key": "drying", "from_anchor": "nope",
                       "to_anchor": "drop",
                       "points": [{"p": 0, "temp_c": 100}, {"p": 1, "temp_c": 200}],
                       "temp_tolerance_c": 2}]},  # bad anchor
        {"segments": [{"key": "drying", "from_anchor": "charge",
                       "to_anchor": "drop",
                       "points": [{"p": 0, "temp_c": 100}, {"p": 1, "temp_c": 200}],
                       "temp_tolerance_c": -1}]},  # bad tolerance
    ]
    for content in bad_cases:
        r = client.post("/api/plans", json={"name": f"坏方案-{content}", "content": content})
        assert r.status_code == 422, content


def test_reversed_anchors_segment_unevaluated(client):
    from app import plan_eval

    content = {
        "segments": [{
            "key": "drying", "label": "d",
            "from_anchor": "drop", "to_anchor": "charge",
            "points": [{"p": 0, "temp_c": 200}, {"p": 1, "temp_c": 100}],
            "temp_tolerance_c": 4.0,
        }]
    }
    events = [
        {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual", "superseded": False},
        {"id": 2, "event_type": "drop", "t_s": 600, "source": "manual", "superseded": False},
    ]
    samples = [{"t_s": t, "bean_temp_c": 150.0} for t in range(0, 601, 30)]
    res = plan_eval.evaluate_plan(samples, events, content)
    seg = res["segments"][0]
    assert seg["verdict"] == "unevaluated"
    assert seg["reasons"][0]["code"] == "anchor_order"


def test_content_sha_and_fingerprint_stable():
    from app import plan_eval

    content = plan_eval.validate_plan_content(_default())
    h1 = plan_eval.content_sha256(content)
    h2 = plan_eval.content_sha256(json_roundtrip(content))
    assert h1 == h2 and len(h1) == 64
    events = [
        {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual", "superseded": False},
        {"id": 2, "event_type": "drop", "t_s": 600, "source": "auto", "superseded": False},
        {"id": 3, "event_type": "drop", "t_s": 590, "source": "manual", "superseded": True},
    ]
    fp = plan_eval.anchor_fingerprint(events)
    # superseded row ignored -> identical to the two-current-row list
    assert fp == plan_eval.anchor_fingerprint(events[:2])
    events2 = events[:2] + [
        {"id": 4, "event_type": "drop", "t_s": 605, "source": "manual", "superseded": False},
        {"id": 2, "event_type": "drop", "t_s": 600, "source": "auto", "superseded": True},
    ]
    assert plan_eval.anchor_fingerprint(events2) != fp


def _default():
    from app.default_plan import DEFAULT_PLAN_CONTENT
    return copy.deepcopy(DEFAULT_PLAN_CONTENT)


def json_roundtrip(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))
