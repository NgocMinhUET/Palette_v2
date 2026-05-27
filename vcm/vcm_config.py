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
# Run 5 post-mortem (diagnose_policy.py):
#   - Policy collapsed to action 11 (QP=32, dROI=+3) with u_task_gt=0.533
#   - Root cause: delay penalty forced RL to minimize bitrate regardless of task quality
#   - Heuristic u_task_hat had no penalty for dROI > 0
#
# Fix (Run 6):
#   - Remove delay penalty entirely (GAMMA_DELAY = 0): VCM objective is task/bitrate,
#     not delay. Real-time constraints should be handled by rate control, not RL reward.
#   - Increase BETA_BITRATE slightly to create meaningful task vs bitrate trade-off.
#   - Keep ROI_TASK_SCALE for cross-layer coupling (vision → codec decision).
#   - Heuristic u_task_hat now includes dROI > 0 penalty (task_utility_estimator.py).
ALPHA_TASK = 1.0
BETA_BITRATE = 0.02         # increased from 0.01 to create task/bitrate trade-off
GAMMA_DELAY = 0.0           # REMOVED: delay penalty was forcing low-bitrate actions
DELTA_LOSS = 0.0
DELAY_TARGET_MS = 500.0     # unused when GAMMA_DELAY=0, kept for reference
ROI_TASK_SCALE = 2.0        # effective alpha for a frame with roi_area=0.5 → 1 + 2*0.5 = 2.0

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
    """RL reward uses u_task_hat only (not u_task_gt / mAP).

    Cross-layer design:
      - Task term is scaled by ROI area so the vision branch of the split-GRU
        network gets a meaningful gradient: a frame with many high-confidence
        detections in a large ROI should be protected by lower QP / negative
        dROI, not sacrificed for cheap delay savings.
      - Delay is penalised only above DELAY_TARGET_MS (overshoot penalty).
        This removes the incentive to maximise QP just to cut bitrate/delay
        at the expense of task quality.
    """
    roi = float(np.clip(obs.get("roi_area", 0.0), 0.0, 1.0))
    task_weight = ALPHA_TASK * (1.0 + ROI_TASK_SCALE * roi)
    overshoot = max(0.0, float(obs["delay_ms"]) - DELAY_TARGET_MS)
    return (
        task_weight * float(obs["u_task_hat"])
        - BETA_BITRATE * float(obs["bitrate_mbps"])
        - GAMMA_DELAY * overshoot
        - DELTA_LOSS * float(obs["loss"])
    )


def compute_oracle_score(obs):
    """Offline upper bound: best action under ground-truth task utility minus transmission costs.

    Uses the same overshoot-delay formulation as compute_vcm_reward so that
    oracle and RL scores are on the same scale during evaluate_vcm.py.
    """
    roi = float(np.clip(obs.get("roi_area", 0.0), 0.0, 1.0))
    task_weight = ALPHA_TASK * (1.0 + ROI_TASK_SCALE * roi)
    overshoot = max(0.0, float(obs["delay_ms"]) - DELAY_TARGET_MS)
    return (
        task_weight * float(obs["u_task_gt"])
        - BETA_BITRATE * float(obs["bitrate_mbps"])
        - GAMMA_DELAY * overshoot
        - DELTA_LOSS * float(obs["loss"])
    )
