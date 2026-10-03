"""Default roast-plan template seeded via POST /api/plans/seed-default.

The template targets the synthetic two-batch demo (see ``synth.py``): the
maillard segment deliberately encloses the 56 s probe dropout, so the UI has
one segment that must render "未评估 · 跨越长断档" rather than a fabricated
pass.  It is only a starting point: it is created as a *draft* and must be
confirmed before any batch may bind to it.
"""
from __future__ import annotations

DEFAULT_PLAN_NAME = "默认埃塞/中浅烘目标曲线 v模板"

DEFAULT_PLAN_CONTENT = {
    "note": (
        "模板：目标段相对 charge / turning_point / first_crack_start / drop "
        "锚点定位；温度为绝对值 °C，控制点 p 为该段锚点实现跨度上的比例位置。"
    ),
    "segments": [
        {
            "key": "drying",
            "label": "脱水期（下豆→回温点）",
            "from_anchor": "charge",
            "to_anchor": "turning_point",
            "points": [
                {"p": 0.0, "temp_c": 176.0},
                {"p": 0.125, "temp_c": 155.0},
                {"p": 0.25, "temp_c": 139.0},
                {"p": 0.375, "temp_c": 122.5},
                {"p": 0.5, "temp_c": 116.0},
                {"p": 0.625, "temp_c": 111.0},
                {"p": 0.75, "temp_c": 108.0},
                {"p": 0.875, "temp_c": 105.0},
                {"p": 1.0, "temp_c": 103.5},
            ],
            "temp_tolerance_c": 4.0,
            "target_duration_s": 55.0,
            "duration_tolerance_s": 20.0,
        },
        {
            "key": "maillard",
            "label": "梅纳/反应期（回温点→一爆开始）",
            "from_anchor": "turning_point",
            "to_anchor": "first_crack_start",
            "points": [
                {"p": 0.0, "temp_c": 103.0},
                {"p": 0.35, "temp_c": 135.0},
                {"p": 0.7, "temp_c": 165.0},
                {"p": 1.0, "temp_c": 192.0},
            ],
            "temp_tolerance_c": 6.0,
            "target_duration_s": 400.0,
            "duration_tolerance_s": 45.0,
        },
        {
            "key": "development",
            "label": "发展期（一爆开始→出锅）",
            "from_anchor": "first_crack_start",
            "to_anchor": "drop",
            "points": [
                {"p": 0.0, "temp_c": 191.0},
                {"p": 0.5, "temp_c": 195.5},
                {"p": 1.0, "temp_c": 200.0},
            ],
            "temp_tolerance_c": 3.0,
            "target_duration_s": 120.0,
            "duration_tolerance_s": 30.0,
        },
    ],
    # Evaluation basis is part of the version document and travels with every
    # binding/export, so later default changes cannot alter old judgments.
    "parameters": {
        "bin_count": 12,
        "min_measured_bin_fraction": 0.75,
        "max_interpolated_bin_fraction": 0.20,
        "long_gap_threshold_s": 45.0,
        "anchor_temp_slop_s": 10.0,
        "min_measured_points": 2,
    },
}
