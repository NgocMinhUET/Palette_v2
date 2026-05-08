# coding=utf-8
"""
Generate synthetic offline VCM profile CSV for trace-driven simulation.

u_task_gt is synthetic ground-truth utility for offline estimator training and oracle baselines only —
not used as online RL reward (controller uses u_task_hat).
"""

import csv
import math
import os

import numpy as np

import vcm_config as cfg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(ROOT, "data", "offline_profiles", "dummy_vcm_profile.csv")

NUM_VIDEOS = 10
FRAMES_PER_VIDEO = 600


def main():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    np_rng = np.random.RandomState(42)

    fields = [
        "video_id",
        "frame_id",
        "bandwidth_mbps",
        "rtt_ms",
        "loss",
        "qp_base",
        "delta_qp_roi",
        "bitrate_mbps",
        "roi_area",
        "obj_count",
        "mean_conf",
        "motion",
        "u_task_gt",
    ]

    total = 0
    with open(OUTPUT_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for vid in range(NUM_VIDEOS):
            for fid in range(FRAMES_PER_VIDEO):
                motion = float(
                    np.clip(
                        np_rng.uniform(5.0, 55.0) + 15.0 * math.sin(fid / 80.0),
                        0.0,
                        cfg.MAX_MOTION,
                    )
                )
                obj_count = int(np_rng.randint(0, 13))
                roi_area = float(
                    np.clip(
                        np_rng.beta(2.0, 6.0) if obj_count > 0 else np_rng.uniform(0.0, 0.05),
                        0.0,
                        1.0,
                    )
                )
                mean_conf = float(
                    np.clip(
                        np_rng.uniform(0.55, 0.95) - 0.003 * motion / cfg.MAX_MOTION,
                        0.1,
                        1.0,
                    )
                )
                bw = float(np.clip(np_rng.uniform(1.5, 9.5), 0.1, cfg.MAX_BANDWIDTH_MBPS))
                rt = float(np_rng.uniform(25.0, 180.0))
                ls = float(np.clip(np_rng.uniform(0.0, 0.08), 0.0, 1.0))
                content_factor = 2.5 + 0.35 * vid + 0.08 * math.sin(fid / 40.0)

                for qp_base in cfg.QP_BASE_SET:
                    for delta_qp_roi in cfg.ROI_QP_OFFSET_SET:
                        bitrate_mbps = content_factor * math.exp(-(qp_base - 24) / 12.0)
                        bitrate_mbps += roi_area * max(0.0, -delta_qp_roi) * 0.05
                        bitrate_mbps = float(np.clip(bitrate_mbps, 0.05, cfg.MAX_BITRATE_MBPS * 1.2))

                        base_quality = 1.0 - (qp_base - 24) / (40 - 24) * 0.35
                        roi_gain = max(0.0, -delta_qp_roi) / 6.0 * 0.10
                        motion_penalty = min(motion / cfg.MAX_MOTION, 1.0) * 0.08
                        loss_penalty = ls * 0.10
                        delay_proxy = max(0.0, bitrate_mbps - bw) / max(cfg.MAX_BANDWIDTH_MBPS, 1e-6)
                        delay_penalty = delay_proxy * 0.07
                        roi_boost = roi_gain * roi_area
                        if obj_count > 0 and 0.05 <= roi_area <= 0.45:
                            roi_boost *= 1.15

                        noise = np_rng.normal(0, 0.012)
                        u_task_gt = (
                            base_quality + roi_boost - motion_penalty - loss_penalty - delay_penalty + noise
                        )
                        u_task_gt = float(np.clip(u_task_gt, 0.0, 1.0))

                        row = {
                            "video_id": vid,
                            "frame_id": fid,
                            "bandwidth_mbps": round(bw, 5),
                            "rtt_ms": round(rt, 3),
                            "loss": round(ls, 5),
                            "qp_base": qp_base,
                            "delta_qp_roi": delta_qp_roi,
                            "bitrate_mbps": round(bitrate_mbps, 5),
                            "roi_area": round(roi_area, 5),
                            "obj_count": obj_count,
                            "mean_conf": round(mean_conf, 5),
                            "motion": round(motion, 5),
                            "u_task_gt": round(u_task_gt, 5),
                        }
                        w.writerow(row)
                        total += 1

    print("Wrote %d rows to %s" % (total, OUTPUT_PATH))


if __name__ == "__main__":
    main()
