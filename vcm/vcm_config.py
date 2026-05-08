# coding=utf-8
"""
CL-ROI-VCM configuration: state dimensions, action space, reward weights, norms.

Not a full WebRTC reproduction of Palette — trace-driven VCM simulator settings.
"""

S_INFO = 9
S_LEN = 6

QP_BASE_SET = [24, 28, 32, 36, 40]
ROI_QP_OFFSET_SET = [-6, -3, 0, 3]

A_DIM = len(QP_BASE_SET) * len(ROI_QP_OFFSET_SET)

# Reward weights (training uses u_task_hat only; u_task_gt is offline/oracle)
ALPHA_TASK = 1.0
BETA_BITRATE = 0.05
GAMMA_DELAY = 0.01
DELTA_LOSS = 0.0

MAX_BANDWIDTH_MBPS = 10.0
MAX_RTT_MS = 500.0
MAX_BITRATE_MBPS = 10.0
MAX_OBJ_COUNT = 20.0
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
    """RL reward uses u_task_hat only (not u_task_gt / mAP)."""
    return (
        ALPHA_TASK * float(obs["u_task_hat"])
        - BETA_BITRATE * float(obs["bitrate_mbps"])
        - GAMMA_DELAY * float(obs["delay_ms"])
        - DELTA_LOSS * float(obs["loss"])
    )


def compute_oracle_score(obs):
    """Offline upper bound: best action under ground-truth task utility minus transmission costs."""
    return (
        ALPHA_TASK * float(obs["u_task_gt"])
        - BETA_BITRATE * float(obs["bitrate_mbps"])
        - GAMMA_DELAY * float(obs["delay_ms"])
        - DELTA_LOSS * float(obs["loss"])
    )
