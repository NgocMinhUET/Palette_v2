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
# Run 4 post-mortem (diagnose_policy.py, ep 2000):
#   ALPHA * u_task_hat  ≈ +0.38
#   GAMMA * delay       ≈ -0.43   ← delay dominated; policy drove QP to 40 to lower bitrate
#
# Fix (Run 5):
#   B1 — reduce GAMMA 10x and penalise only the overshoot above DELAY_TARGET_MS
#          so the agent is not rewarded for blindly compressing harder.
#   B2 — scale task reward by (1 + ROI_TASK_SCALE * roi_area) so the vision branch
#          gets a strong gradient: high-ROI frames are worth more quality investment.
#          This is the key cross-layer coupling for VCM.
ALPHA_TASK = 1.0
BETA_BITRATE = 0.01
GAMMA_DELAY = 1e-4          # was 0.001; only overshoot above DELAY_TARGET_MS is penalised
DELTA_LOSS = 0.0
DELAY_TARGET_MS = 250.0     # frames delivered under this threshold pay zero delay penalty
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
