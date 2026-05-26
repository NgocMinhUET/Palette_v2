# coding=utf-8
"""
Profile inspection utility for CL-ROI-VCM.

Answers three diagnostic questions before any retraining:

  Q1. Does u_task_gt actually respond to ROI offset (dROI)?
      If not, the vision branch of the policy has no gradient signal.

  Q2. Does u_task_gt respond to QP_base?
      If the range is narrow (<0.05), reward shaping must compensate.

  Q3. What is the (delay, bitrate) distribution vs (QP, dROI)?
      Tells us whether GAMMA_DELAY really dominates at current weights.

Output: a compact summary table + optional JSON for scripted use.

Usage:
    python vcm/inspect_profile.py \
        --profile_csv data/offline_profiles/bdd100k_x265_intra_profile.csv \
        [--out_json results/vcm_logs_x265_v3/profile_inspect.json]
"""

from __future__ import print_function

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VCM_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _VCM_ROOT)

import vcm_config as cfg  # noqa: E402


# ---------------------------------------------------------------------------
# Current reward weights (replicated here so the printout is self-contained)
# ---------------------------------------------------------------------------
ALPHA = cfg.ALPHA_TASK
BETA = cfg.BETA_BITRATE
GAMMA = cfg.GAMMA_DELAY


def _resolve(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_REPO_ROOT, p))


def load_profile(path):
    rows = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    "video_id":    int(r["video_id"]),
                    "frame_id":    int(r["frame_id"]),
                    "qp_base":     int(r["qp_base"]),
                    "delta_qp_roi": int(r["delta_qp_roi"]),
                    "bitrate_mbps": float(r["bitrate_mbps"]),
                    "u_task_gt":   float(r["u_task_gt"]),
                    "roi_area":    float(r.get("roi_area", 0.0) or 0.0),
                    "obj_count":   float(r.get("obj_count", 0.0) or 0.0),
                    "mean_conf":   float(r.get("mean_conf", 0.0) or 0.0),
                    "motion":      float(r.get("motion", 0.0) or 0.0),
                })
            except (KeyError, ValueError):
                continue
    return rows


# ---------------------------------------------------------------------------
# Per-(qp_base, delta_qp_roi) aggregates
# ---------------------------------------------------------------------------
def action_stats(rows):
    """Mean u_task_gt and bitrate for each (qp_base, dROI) pair."""
    buckets = defaultdict(lambda: {"u": [], "br": []})
    for r in rows:
        key = (r["qp_base"], r["delta_qp_roi"])
        buckets[key]["u"].append(r["u_task_gt"])
        buckets[key]["br"].append(r["bitrate_mbps"])

    result = {}
    for (qp, dr), v in sorted(buckets.items()):
        a_id = cfg.encode_action(qp, dr)
        result[a_id] = {
            "qp_base": qp,
            "delta_qp_roi": dr,
            "action_id": a_id,
            "n": len(v["u"]),
            "u_task_gt_mean": float(np.mean(v["u"])),
            "u_task_gt_std":  float(np.std(v["u"])),
            "bitrate_mbps_mean": float(np.mean(v["br"])),
            "bitrate_mbps_std":  float(np.std(v["br"])),
        }
    return result


# ---------------------------------------------------------------------------
# Reward simulation: what would the reward be per action under current weights?
# (Using a typical delay derived from bitrate / bandwidth)
# ---------------------------------------------------------------------------
def simulated_reward(stats, bw_mbps=5.0, rtt_ms=80.0):
    """
    Approximate reward = ALPHA*u_hat - BETA*bitrate - GAMMA*delay
    where delay = rtt + (bitrate / bw) * 1000  [ms]
    and u_hat ≈ u_task_gt (oracle proxy, best case for task signal).
    """
    result = {}
    for a_id, s in stats.items():
        br = s["bitrate_mbps_mean"]
        delay = rtt_ms + (br / bw_mbps) * 1000.0
        reward_current = ALPHA * s["u_task_gt_mean"] - BETA * br - GAMMA * delay
        result[a_id] = {
            **s,
            "sim_delay_ms": delay,
            "sim_reward_current": reward_current,
        }
    return result


# ---------------------------------------------------------------------------
# Q1/Q2: sensitivity analysis
# ---------------------------------------------------------------------------
def roi_sensitivity(stats):
    """For each QP_base, how much does u_task_gt change across dROI values?"""
    by_qp = defaultdict(list)
    for s in stats.values():
        by_qp[s["qp_base"]].append((s["delta_qp_roi"], s["u_task_gt_mean"]))
    out = {}
    for qp, pairs in sorted(by_qp.items()):
        pairs.sort()
        us = [u for _, u in pairs]
        out[qp] = {
            "dROI_values":  [d for d, _ in pairs],
            "u_task_gt":    [round(u, 4) for u in us],
            "u_range":      round(max(us) - min(us), 4),
            "signal_ok":    (max(us) - min(us)) > 0.02,
        }
    return out


def qp_sensitivity(stats):
    """For a fixed dROI=0, how does u_task_gt change across QP_base?"""
    pairs = [(s["qp_base"], s["u_task_gt_mean"])
             for s in stats.values() if s["delta_qp_roi"] == 0]
    pairs.sort()
    us = [u for _, u in pairs]
    if not pairs:
        return {"error": "no dROI=0 rows found"}
    return {
        "QP_values":  [qp for qp, _ in pairs],
        "u_task_gt":  [round(u, 4) for u in us],
        "u_range":    round(max(us) - min(us), 4),
        "signal_ok":  (max(us) - min(us)) > 0.05,
    }


# ---------------------------------------------------------------------------
# Dominant-action analysis: which action wins under current vs proposed reward?
# ---------------------------------------------------------------------------
def reward_comparison(sim, bw_mbps=5.0):
    """
    Compare three reward formulations:
      current  : alpha*u - beta*br - gamma*delay         (gamma=0.001)
      b1       : alpha*u - beta*br - gamma_new*overshoot (gamma=1e-4, tgt=250ms)
      b2       : (1+2*roi)*u - beta*br - gamma_new*overshoot
    """
    GAMMA_NEW = 1e-4
    DELAY_TGT = 250.0
    # Use a representative ROI = 0.20 for b2 illustration
    ROI_REPR = 0.20

    rows_out = []
    for a_id in sorted(sim.keys()):
        s = sim[a_id]
        br = s["bitrate_mbps_mean"]
        delay = s["sim_delay_ms"]
        u = s["u_task_gt_mean"]
        overshoot = max(0.0, delay - DELAY_TGT)

        r_current = ALPHA * u - BETA * br - GAMMA * delay
        r_b1 = ALPHA * u - BETA * br - GAMMA_NEW * overshoot
        r_b2 = (1.0 + 2.0 * ROI_REPR) * u - BETA * br - GAMMA_NEW * overshoot

        rows_out.append({
            "a_id": a_id,
            "qp": s["qp_base"],
            "dROI": s["delta_qp_roi"],
            "u": round(u, 3),
            "br": round(br, 2),
            "delay": round(delay, 0),
            "r_current": round(r_current, 4),
            "r_b1": round(r_b1, 4),
            "r_b2": round(r_b2, 4),
        })
    best_current = max(rows_out, key=lambda x: x["r_current"])
    best_b1 = max(rows_out, key=lambda x: x["r_b1"])
    best_b2 = max(rows_out, key=lambda x: x["r_b2"])
    return rows_out, best_current, best_b1, best_b2


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------
def print_report(stats, roi_sens, qp_sens, sim, cmp_rows, best_cur, best_b1, best_b2):
    HR = "-" * 72

    print(HR)
    print("PROFILE INSPECTION REPORT")
    print(HR)

    # Q1: ROI signal
    print("\n[Q1] u_task_gt sensitivity to dROI (per QP_base):")
    print("  %5s  %35s  %7s  %s" % ("QP", "u_task_gt per dROI", "u_range", "signal?"))
    for qp, v in roi_sens.items():
        vals_str = "  ".join(["%+d→%.3f" % (d, u)
                               for d, u in zip(v["dROI_values"], v["u_task_gt"])])
        ok = "YES" if v["signal_ok"] else "NO (< 0.02)"
        print("  %5d  %-35s  %7.4f  %s" % (qp, vals_str, v["u_range"], ok))

    # Q2: QP signal
    print("\n[Q2] u_task_gt sensitivity to QP_base (dROI=0 only):")
    if "error" in qp_sens:
        print("  ERROR:", qp_sens["error"])
    else:
        print("  QP:", qp_sens["QP_values"])
        print("  u_task_gt:", qp_sens["u_task_gt"])
        print("  range = %.4f  signal? %s" % (qp_sens["u_range"],
                                               "YES" if qp_sens["signal_ok"] else "NO (< 0.05)"))

    # Q3: reward dominance
    print("\n[Q3] Reward component breakdown at bw=5 Mbps, rtt=80 ms:")
    print("  %4s  %5s %6s  %7s  %7s  %8s  %-10s  %-10s  %-10s" % (
        "a_id", "QP", "dROI", "u", "br(Mb)", "delay(ms)",
        "R_current", "R_B1", "R_B2"))
    for r in cmp_rows:
        marker = " <-- best" if r["a_id"] == best_cur["a_id"] else ""
        marker_b1 = "<b1" if r["a_id"] == best_b1["a_id"] else ""
        marker_b2 = "<b2" if r["a_id"] == best_b2["a_id"] else ""
        print("  %4d  %5d %6d  %7.3f  %7.2f  %9.0f  %10.4f  %10.4f  %10.4f  %s%s%s" % (
            r["a_id"], r["qp"], r["dROI"], r["u"], r["br"], r["delay"],
            r["r_current"], r["r_b1"], r["r_b2"], marker, marker_b1, marker_b2))

    print()
    print("  Current reward WINNER : a=%d  QP=%d dROI=%+d  u=%.3f" % (
        best_cur["a_id"], best_cur["qp"], best_cur["dROI"], best_cur["u"]))
    print("  B1 reward WINNER      : a=%d  QP=%d dROI=%+d  u=%.3f" % (
        best_b1["a_id"], best_b1["qp"], best_b1["dROI"], best_b1["u"]))
    print("  B2 reward WINNER      : a=%d  QP=%d dROI=%+d  u=%.3f" % (
        best_b2["a_id"], best_b2["qp"], best_b2["dROI"], best_b2["u"]))
    print()

    # Verdict
    print("[Reward rebalance verdict]")
    if best_cur["qp"] == max(cfg.QP_BASE_SET) and best_cur["dROI"] >= 0:
        print("  CONFIRM: current reward drives policy toward high-QP / anti-ROI actions.")
    if best_b1["qp"] < best_cur["qp"] or best_b1["dROI"] < best_cur["dROI"]:
        print("  B1 fix: winner shifts toward lower QP or negative dROI — better for VCM.")
    else:
        print("  B1 fix: winner unchanged — profile may lack ROI/QP signal.")
    if best_b2["dROI"] < best_cur["dROI"]:
        print("  B2 fix: ROI-weighting moves winner to negative dROI — VCM signal ON.")
    print(HR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile_csv",
                    default="data/offline_profiles/bdd100k_x265_intra_profile.csv")
    ap.add_argument("--bw_mbps", type=float, default=5.0)
    ap.add_argument("--rtt_ms", type=float, default=80.0)
    ap.add_argument("--out_json", default="")
    args = ap.parse_args()

    profile_path = _resolve(args.profile_csv)
    if not os.path.isfile(profile_path):
        print("ERROR: profile not found:", profile_path)
        sys.exit(1)

    rows = load_profile(profile_path)
    print("[inspect] loaded %d profile rows from %s" % (len(rows), profile_path))

    stats = action_stats(rows)
    roi_sens = roi_sensitivity(stats)
    qp_sens = qp_sensitivity(stats)
    sim = simulated_reward(stats, bw_mbps=args.bw_mbps, rtt_ms=args.rtt_ms)
    cmp_rows, best_cur, best_b1, best_b2 = reward_comparison(sim, bw_mbps=args.bw_mbps)

    print_report(stats, roi_sens, qp_sens, sim, cmp_rows, best_cur, best_b1, best_b2)

    if args.out_json:
        out_path = _resolve(args.out_json)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({
                "profile_csv": profile_path,
                "n_rows": len(rows),
                "action_stats": {str(k): v for k, v in stats.items()},
                "roi_sensitivity": {str(k): v for k, v in roi_sens.items()},
                "qp_sensitivity": qp_sens,
                "reward_comparison": cmp_rows,
                "best_action_current_reward": best_cur,
                "best_action_b1_reward": best_b1,
                "best_action_b2_reward": best_b2,
            }, f, indent=2)
        print("[inspect] wrote", out_path)


if __name__ == "__main__":
    main()
