# coding=utf-8
"""
Interpretable baselines for CL-ROI-VCM evaluation.

References (use in paper Related Work / Experiments):

  [Duan20] L.-Y. Duan et al., "Video Coding for Machines: A Paradigm of
      Collaborative Compression and Intelligent Analytics," IEEE TIP, 2020.
      Non-collaborative baseline = conventional video coding, then machine
      analytics (no closed-loop task/network control).

  [CTC] MPEG VCM Common Test Conditions and Evaluation Methodology.
      Conventional *video anchor*: standard codec (VTM/HEVC), six QP points,
      task metric mAP@[0.5:0.95], dROI/task-aware control disabled.

  [CAV] CompressAI-Vision (InterDigital; arXiv:2509.20777): open evaluation
      platform aligned with MPEG VCM/FCM CTC anchor pipelines.

  [ROI25] Region-of-interest retargeting for VCM (JVIP 2025): reports BD-Rate
      vs the same CTC anchor under AI/RA/LD configurations.

Policies:

  vcm_pure / vcm_ctc_anchor
      MPEG VCM CTC conventional video anchor (task-agnostic, no cross-layer):
        - RD curve: preset ladder_uniform in evaluate_bd_acc.py (6 QP, dROI=0)
        - Trace sim: one operating point on that ladder — QP whose mean profile
          bitrate best matches mean trace bandwidth (capacity-matched anchor point)

  palette_xlayer
      Palette-style cross-layer (IEEE Palette): network + content -> QP_base,
      dROI=0 (no ROI cross-layer).

  cl_roi_rule
      Full CL-ROI-VCM rule: bw -> QP and roi_area -> dROI.
"""

from __future__ import print_function

import csv
import os

import vcm_config as cfg

# CTC anchor: no ROI QP offset (task-agnostic video path, Track-2 style)
VCM_CTC_ANCHOR_DROI = 0

# Surrogate six-point AI ladder (libx265 intra profile); maps to CTC "six QPs"
VCM_CTC_QP_LADDER = tuple(cfg.QP_BASE_SET)

# Fallback mid-ladder QP if profile/trace unavailable
VCM_CTC_DEFAULT_QP = int(VCM_CTC_QP_LADDER[len(VCM_CTC_QP_LADDER) // 2])

# Legacy aliases (uniform_qp in test_vcm uses the default anchor QP)
VCM_PURE_QP = VCM_CTC_DEFAULT_QP
VCM_PURE_DROI = VCM_CTC_ANCHOR_DROI


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_path(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_repo_root(), p))


def load_ctc_anchor_ladder_stats(profile_csv):
    """
    Mean bitrate / task utility per QP on the CTC anchor ladder (dROI=0).

    Returns dict: qp_base -> {"bitrate_mbps": float, "u_task_gt": float, "n": int}
    """
    path = _resolve_path(profile_csv)
    acc = {}
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            if int(row["delta_qp_roi"]) != VCM_CTC_ANCHOR_DROI:
                continue
            qp = int(row["qp_base"])
            if qp not in VCM_CTC_QP_LADDER:
                continue
            bucket = acc.setdefault(qp, {"br": [], "ut": []})
            bucket["br"].append(float(row["bitrate_mbps"]))
            bucket["ut"].append(float(row["u_task_gt"]))
    out = {}
    for qp in VCM_CTC_QP_LADDER:
        if qp not in acc:
            continue
        b = acc[qp]
        out[qp] = {
            "bitrate_mbps": sum(b["br"]) / len(b["br"]),
            "u_task_gt": sum(b["ut"]) / len(b["ut"]),
            "n": len(b["br"]),
        }
    return out


def mean_trace_bandwidth_mbps(all_cooked_bw):
    """Average available bandwidth (Mbps) over all loaded trace files."""
    vals = []
    for series in all_cooked_bw or []:
        for v in series:
            vals.append(float(v))
    if not vals:
        return None
    return sum(vals) / len(vals)


def resolve_ctc_anchor_qp(profile_csv, target_bw_mbps=None, all_cooked_bw=None):
    """
    Pick one QP on the MPEG VCM CTC anchor ladder (task-agnostic).

    If target_bw_mbps is None, use mean trace bandwidth. Among ladder points
    with mean profile bitrate <= target, choose the highest QP (lowest rate);
    if none fit, choose the lowest QP (highest quality).
    """
    stats = load_ctc_anchor_ladder_stats(profile_csv)
    if not stats:
        return VCM_CTC_DEFAULT_QP

    if target_bw_mbps is None:
        target_bw_mbps = mean_trace_bandwidth_mbps(all_cooked_bw)
    if target_bw_mbps is None:
        return VCM_CTC_DEFAULT_QP

    target = float(target_bw_mbps)
    feasible = [
        qp for qp in VCM_CTC_QP_LADDER
        if qp in stats and stats[qp]["bitrate_mbps"] <= target
    ]
    if feasible:
        return int(max(feasible))
    # Congested link: use lowest-QP (highest quality) anchor point available
    return int(min(qp for qp in VCM_CTC_QP_LADDER if qp in stats))


def _qp_index(qp):
    qp = int(qp)
    if qp in cfg.QP_BASE_SET:
        return cfg.QP_BASE_SET.index(qp)
    return min(range(len(cfg.QP_BASE_SET)),
               key=lambda i: abs(cfg.QP_BASE_SET[i] - qp))


def _smooth_qp_step(target_qp, last_qp):
    """At most one step on QP_BASE_SET per frame (Palette smoothness idea)."""
    ti = _qp_index(target_qp)
    li = _qp_index(last_qp)
    if ti > li + 1:
        ti = li + 1
    elif ti < li - 1:
        ti = li - 1
    return cfg.QP_BASE_SET[ti]


def select_qp_from_bandwidth(bw_mbps):
    """Map available bandwidth to HEVC QP_base (higher QP when congested)."""
    bw = float(bw_mbps)
    if bw < 3.0:
        return 40
    if bw < 5.5:
        return 36
    if bw < 8.0:
        return 32
    if bw < 11.0:
        return 28
    return 24


def palette_xlayer_action(feat, last_qp):
    """
    Palette-style cross-layer: network + content -> QP_base, no ROI offset.

    feat: dict from Environment.current_decision_features()
    last_qp: previous applied qp_base (int)
    """
    bw = float(feat["bandwidth_mbps"])
    loss = float(feat.get("loss", 0.0))
    motion = float(feat.get("motion", 0.0))

    qp = select_qp_from_bandwidth(bw)
    if loss > 0.05 and qp < 40:
        qp = cfg.QP_BASE_SET[min(_qp_index(qp) + 1, len(cfg.QP_BASE_SET) - 1)]
    if motion > 35.0 and _qp_index(qp) > 0:
        qp = cfg.QP_BASE_SET[_qp_index(qp) - 1]

    qp = _smooth_qp_step(qp, last_qp)
    return cfg.encode_action(qp, 0), qp, 0


def cl_roi_rule_action(feat, last_qp):
    """Full CL-ROI-VCM cross-layer rule: network -> QP, vision (roi) -> dROI."""
    bw = float(feat["bandwidth_mbps"])
    roi = float(feat.get("roi_area", 0.0))

    qp = select_qp_from_bandwidth(bw)
    qp = _smooth_qp_step(qp, last_qp)

    if roi >= 0.30:
        droi = -3
    elif roi >= 0.12:
        droi = 0
    else:
        droi = 3

    return cfg.encode_action(qp, droi), qp, droi


def vcm_pure_action(anchor_qp=None):
    """
    MPEG VCM CTC conventional video anchor — single operating point.

    Use evaluate_bd_acc preset ``ladder_uniform`` (alias ``vcm_ctc_anchor``) for
    the full six-QP RD curve per [CTC]. For trace simulation, pass anchor_qp from
    resolve_ctc_anchor_qp() so the point lies on that ladder.
    """
    qp = int(anchor_qp if anchor_qp is not None else VCM_CTC_DEFAULT_QP)
    droi = VCM_CTC_ANCHOR_DROI
    return cfg.encode_action(qp, droi), qp, droi


# Alias for paper naming
vcm_ctc_anchor_action = vcm_pure_action
