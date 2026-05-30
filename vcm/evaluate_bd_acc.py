# coding=utf-8
"""
BD-Acc / BD-Rate evaluation from offline VCM profile CSV.

VCM papers compare methods with **task accuracy vs bitrate** curves (often mAP or
wAP), then report **Bjontegaard Delta Rate (BD-Rate)** at equal accuracy.

This script does NOT re-encode video. It aggregates rows already in the profile:
  - Per (qp_base, delta_qp_roi): mean u_task_gt, mean bitrate_mbps
  - Per video_id (sequence): same aggregates → one RD point cloud per clip
  - BD-Rate between two "methods" defined as sets of operating points

Typical methods (built-in presets):
  ladder_uniform / vcm_ctc_anchor
      MPEG VCM CTC conventional video anchor [CTC]: six QP points, dROI=0,
      task-agnostic (Duan20 non-collaborative / CompressAI-Vision anchor path)
  ladder_roi_prot  — same QP, dROI=-3 when ROI present (matches encode_x265 bias)
  all_actions      — all 20 (qp, dROI) pairs in profile

Limitations (read before citing numbers):
  - HF image profile (bdd100k_x265_intra): video_id=0, frames are INDEPENDENT
    images, not temporal sequences. Use build_real_profile (video clips) for
    sequence BD-Acc.
  - RL / Oracle controllers need per-frame action traces; this script only
    covers **fixed operating-point** curves unless you pass --actions_csv.

Usage:
  python vcm/evaluate_bd_acc.py \
    --profile_csv data/offline_profiles/bdd100k_x265_intra_v2_profile.csv

  python vcm/evaluate_bd_acc.py \
    --profile_csv data/offline_profiles/real_bdd_profile.csv \
    --compare ladder_uniform:proposed_ladder
"""

from __future__ import print_function

import argparse
import csv
import json
import os
import sys

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VCM = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO)
sys.path.insert(0, _VCM)

import vcm_config as cfg  # noqa: E402


def _resolve(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_REPO, p))


def load_profile(path):
    rows = []
    with open(path, "r", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "video_id": int(r["video_id"]),
                "frame_id": int(r["frame_id"]),
                "qp_base": int(r["qp_base"]),
                "delta_qp_roi": int(r["delta_qp_roi"]),
                "bitrate_mbps": float(r["bitrate_mbps"]),
                "u_task_gt": float(r["u_task_gt"]),
                "roi_area": float(r.get("roi_area", 0) or 0),
            })
    return rows


def preset_points(name):
    """Return list of (qp_base, delta_qp_roi) for a named method."""
    if name in ("ladder_uniform", "vcm_ctc_anchor", "vcm_pure"):
        return [(qp, 0) for qp in cfg.QP_BASE_SET]
    if name == "ladder_roi_prot":
        return [(qp, -3) for qp in cfg.QP_BASE_SET]
    if name == "ladder_degrade_roi":
        return [(qp, 3) for qp in cfg.QP_BASE_SET]
    if name == "all_actions":
        out = []
        for qp in cfg.QP_BASE_SET:
            for dr in cfg.ROI_QP_OFFSET_SET:
                out.append((qp, dr))
        return out
    raise ValueError("Unknown preset: %s" % name)


def aggregate_rd(rows, points, group_key=None):
    """
    Aggregate (bitrate, accuracy) for operating points in `points`.
    group_key: None = global; 'video_id' = per sequence.
    """
    point_set = set((int(q), int(d)) for q, d in points)
    filt = [r for r in rows if (r["qp_base"], r["delta_qp_roi"]) in point_set]
    if not filt:
        return []

    if group_key is None:
        buckets = {"all": filt}
    else:
        buckets = {}
        for r in filt:
            k = r[group_key]
            buckets.setdefault(k, []).append(r)

    curves = []
    for key, sub in sorted(buckets.items()):
        by_op = {}
        for r in sub:
            op = (r["qp_base"], r["delta_qp_roi"])
            by_op.setdefault(op, []).append(r)
        pts = []
        for op in sorted(by_op.keys()):
            chunk = by_op[op]
            br = float(np.mean([x["bitrate_mbps"] for x in chunk]))
            acc = float(np.mean([x["u_task_gt"] for x in chunk]))
            pts.append({
                "qp_base": op[0],
                "delta_qp_roi": op[1],
                "action_id": cfg.encode_action(op[0], op[1]),
                "bitrate_mbps": br,
                "accuracy": acc,
                "n_frames": len(chunk),
            })
        pts.sort(key=lambda x: x["bitrate_mbps"])
        curves.append({"group": key, "points": pts})
    return curves


def bd_rate(anchor_pts, test_pts, accuracy_key="accuracy"):
    """
    Bjontegaard Delta Rate (%) of TEST relative to ANCHOR at equal accuracy.

    Sign convention (matches VCEG / VTM-style reporting):
      Negative BD-Rate => TEST uses LESS bitrate at same accuracy (better).
      Positive BD-Rate => TEST uses MORE bitrate at same accuracy (worse).

    Uses piecewise-linear interpolation in log2(rate) vs accuracy.
    """
    def _curve(pts):
        br = np.array([max(p["bitrate_mbps"], 1e-6) for p in pts], dtype=np.float64)
        acc = np.array([p[accuracy_key] for p in pts], dtype=np.float64)
        idx = np.argsort(acc)
        return acc[idx], np.log2(br[idx])

    acc_a, log_r_a = _curve(anchor_pts)
    acc_t, log_r_t = _curve(test_pts)
    if len(acc_a) < 2 or len(acc_t) < 2:
        return float("nan"), "need >= 2 points per curve"

    lo = max(acc_a.min(), acc_t.min())
    hi = min(acc_a.max(), acc_t.max())
    if hi - lo < 1e-4:
        return float("nan"), "accuracy overlap too small"

    grid = np.linspace(lo, hi, 50)
    log_r_a_i = np.interp(grid, acc_a, log_r_a)
    log_r_t_i = np.interp(grid, acc_t, log_r_t)
    # BD-Rate formula: mean difference in log-rate * 100 / ln(2) ... 
    # Standard: 100 * (2^(mean(log2_r_test - log2_r_anchor)) - 1) with integral
    diff = log_r_t_i - log_r_a_i
    bd = 100.0 * (np.mean(2.0 ** diff) - 1.0)
    return float(bd), "ok"


def print_curve(curve, title):
    print("\n--- %s (group=%s) ---" % (title, curve["group"]))
    print("  %6s  %6s  %6s  %10s  %10s  %6s" % (
        "a_id", "QP", "dROI", "bitrate", "accuracy", "n"))
    for p in curve["points"]:
        print("  %6d  %6d  %6d  %10.3f  %10.4f  %6d" % (
            p["action_id"], p["qp_base"], p["delta_qp_roi"],
            p["bitrate_mbps"], p["accuracy"], p["n_frames"]))


def main():
    ap = argparse.ArgumentParser(description="BD-Acc / BD-Rate from offline profile CSV")
    ap.add_argument("--profile_csv", required=True)
    ap.add_argument(
        "--compare",
        default="ladder_uniform:ladder_roi_prot",
        help="anchor:test preset names, colon-separated",
    )
    ap.add_argument("--per_sequence", action="store_true",
                    help="Also print per video_id curves (needs real clips profile)")
    ap.add_argument("--out_json", default="")
    args = ap.parse_args()

    path = _resolve(args.profile_csv)
    rows = load_profile(path)
    n_vid = len(set(r["video_id"] for r in rows))
    n_frm = len(set((r["video_id"], r["frame_id"]) for r in rows))
    print("Profile: %s" % path)
    print("  rows=%d  sequences(video_id)=%d  unique_frames=%d" % (len(rows), n_vid, n_frm))
    if n_vid <= 1:
        print("  WARNING: single video_id — likely HF image backend, NOT video sequences.")
        print("  BD curves here characterize **codec operating points**, not temporal adaptation.")
        print("  For sequence BD-Acc: python -m vcm.profile.build_real_profile ...")

    parts = args.compare.split(":")
    if len(parts) != 2:
        raise SystemExit("--compare must be anchor:test (two preset names)")
    anchor_name, test_name = parts[0].strip(), parts[1].strip()
    anchor_pts_def = preset_points(anchor_name)
    test_pts_def = preset_points(test_name)

    anchor_curves = aggregate_rd(rows, anchor_pts_def, group_key=None)
    test_curves = aggregate_rd(rows, test_pts_def, group_key=None)
    if not anchor_curves or not test_curves:
        raise SystemExit("Empty curve — check profile has required (qp, dROI) rows")

    print_curve(anchor_curves[0], "ANCHOR: " + anchor_name)
    print_curve(test_curves[0], "TEST: " + test_name)

    bd, msg = bd_rate(anchor_curves[0]["points"], test_curves[0]["points"])
    print("\n=== BD-Rate (%% of TEST vs ANCHOR at equal accuracy) ===")
    print("  %s vs %s : BD-Rate = %+.2f%%  (%s)" % (test_name, anchor_name, bd, msg))
    if bd < 0:
        print("  => TEST saves bitrate (better compression efficiency than anchor).")
    elif bd > 0:
        print("  => TEST needs more bitrate (worse efficiency than anchor at same accuracy).")

    per_seq_bd = []
    if args.per_sequence and n_vid > 1:
        print("\n=== Per-sequence BD-Rate ===")
        a_by_vid = {c["group"]: c for c in aggregate_rd(rows, anchor_pts_def, "video_id")}
        t_by_vid = {c["group"]: c for c in aggregate_rd(rows, test_pts_def, "video_id")}
        for vid in sorted(a_by_vid.keys()):
            if vid not in t_by_vid:
                continue
            bdv, m = bd_rate(a_by_vid[vid]["points"], t_by_vid[vid]["points"])
            per_seq_bd.append(bdv)
            print("  video_id=%s  BD-Rate=%+.2f%%  (%s)" % (vid, bdv, m))
        if per_seq_bd:
            print("  mean per-sequence BD-Rate = %+.2f%%" % float(np.mean(per_seq_bd)))

    report = {
        "profile_csv": path,
        "n_sequences": n_vid,
        "anchor": anchor_name,
        "test": test_name,
        "global_bd_rate_pct": bd,
        "anchor_curve": anchor_curves[0],
        "test_curve": test_curves[0],
    }
    if args.out_json:
        out_path = _resolve(args.out_json)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
        print("\nWrote", out_path)


if __name__ == "__main__":
    main()
