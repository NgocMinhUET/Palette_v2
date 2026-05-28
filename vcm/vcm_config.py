# coding=utf-8
"""
CL-ROI-VCM configuration: state dimensions, action space, reward weights, norms.

Not a full WebRTC reproduction of Palette — trace-driven VCM simulator settings.
"""

import numpy as np

S_INFO = 9
S_LEN = 6

QP_BASE_SET = [24, 28, 32, 36, 40]
ROI_QP_OFFSET_SET = [-6, -3, 0, 3]

A_DIM = len(QP_BASE_SET) * len(ROI_QP_OFFSET_SET)

# Reward weights (training uses u_task_hat only; u_task_gt is offline/oracle)
#
# Run 6 post-mortem:
#   - Policy collapsed to action 3 (QP=24, dROI=+3) with u_task_gt=0.708
#   - Root cause: action 3 has highest u_task_gt in profile, RL learns "always pick 3"
#   - Missing: STATE-DEPENDENT optimal action (cross-layer coupling)
#
# Fix (Run 7) — explicit cross-layer coupling in reward:
#   1. CONGESTION penalty: when bitrate > bandwidth, heavy penalty
#      → forces QP adaptation to network conditions
#   2. ROI_MISMATCH penalty: when roi_area high AND dROI > 0, penalty
#      → forces dROI adaptation to vision content
#   These create state-dependent optimal actions (the core of cross-layer).
ALPHA_TASK = 1.0
BETA_BITRATE = 0.015        # moderate bitrate penalty
GAMMA_CONGESTION = 0.08     # NEW: penalty per Mbps when bitrate > bandwidth
GAMMA_ROI_MISMATCH = 0.3    # NEW: penalty for (roi_area × max(0, dROI))
DELTA_LOSS = 0.0
ROI_TASK_SCALE = 1.5        # reduced: main ROI coupling now via GAMMA_ROI_MISMATCH

MAX_BANDWIDTH_MBPS = 15.0
MAX_RTT_MS = 1000.0
MAX_BITRATE_MBPS = 15.0
MAX_OBJ_COUNT = 40.0
MAX_MOTION = 100.0

QP_MIN = min(QP_BASE_SET)
QP_MAX = max(QP_BASE_SET)


def decode_action(action_id):
    """Map flat action index to (qp_base, delta_qp_roi)."""
    n_roi = len(ROI_QP_OFFSET_SET)
    qp_idx = action_id // n_roi
    roi_idx = action_id % n_roi
    return QP_BASE_SET[qp_idx], ROI_QP_OFFSET_SET[roi_idx]


def encode_action(qp_base, delta_qp_roi):
    """Inverse of decode_action for lookups."""
    qp_idx = QP_BASE_SET.index(qp_base)
    roi_idx = ROI_QP_OFFSET_SET.index(delta_qp_roi)
    return qp_idx * len(ROI_QP_OFFSET_SET) + roi_idx


def compute_vcm_reward(obs):
    """RL reward with explicit cross-layer coupling.

    Cross-layer design (Run 7):
      1. CONGESTION penalty: bitrate exceeding bandwidth causes heavy penalty.
         Effect: when bw is low, RL must choose higher QP (lower bitrate).
         When bw is high, RL can choose lower QP (higher quality).
         → Network branch learns to adapt QP to bandwidth.

      2. ROI_MISMATCH penalty: when roi_area is high AND dROI > 0, penalty.
         Effect: important ROI frames must not be degraded with positive dROI.
         Low-ROI frames can use dROI > 0 to save bitrate.
         → Vision branch learns to adapt dROI to ROI content.

      These two couplings create STATE-DEPENDENT optimal actions, which is
      the essence of cross-layer adaptation for VCM.
    """
    bw = float(obs.get("bandwidth_mbps", 10.0))
    bitrate = float(obs["bitrate_mbps"])
    roi = float(np.clip(obs.get("roi_area", 0.0), 0.0, 1.0))
    dROI = float(obs.get("delta_qp_roi", 0))

    task_weight = ALPHA_TASK * (1.0 + ROI_TASK_SCALE * roi)

    # Cross-layer coupling 1: Network → QP (congestion penalty)
    congestion = max(0.0, bitrate - bw)

    # Cross-layer coupling 2: Vision → dROI (ROI mismatch penalty)
    # High ROI + positive dROI = bad (degrading important content)
    roi_mismatch = roi * max(0.0, dROI)

    return (
        task_weight * float(obs["u_task_hat"])
        - BETA_BITRATE * bitrate
        - GAMMA_CONGESTION * congestion
        - GAMMA_ROI_MISMATCH * roi_mismatch
        - DELTA_LOSS * float(obs["loss"])
    )


def compute_oracle_score(obs):
    """Offline upper bound: best action under ground-truth task utility.

    Uses the same cross-layer coupling as compute_vcm_reward so that
    oracle and RL scores are on the same scale during evaluate_vcm.py.
    """
    bw = float(obs.get("bandwidth_mbps", 10.0))
    bitrate = float(obs["bitrate_mbps"])
    roi = float(np.clip(obs.get("roi_area", 0.0), 0.0, 1.0))
    dROI = float(obs.get("delta_qp_roi", 0))

    task_weight = ALPHA_TASK * (1.0 + ROI_TASK_SCALE * roi)
    congestion = max(0.0, bitrate - bw)
    roi_mismatch = roi * max(0.0, dROI)

    return (
        task_weight * float(obs["u_task_gt"])
        - BETA_BITRATE * bitrate
        - GAMMA_CONGESTION * congestion
        - GAMMA_ROI_MISMATCH * roi_mismatch
        - DELTA_LOSS * float(obs["loss"])
    )
