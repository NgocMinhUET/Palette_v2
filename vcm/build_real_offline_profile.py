# coding=utf-8
"""
CL-ROI-VCM: Build real offline profile CSV from various dataset backends.

Backends:
    hf_bdd100k   — Hugging Face `dgural/bdd100k` (image/keyframe, no registration)
    video_clips  — local mp4/mov clips via vcm/profile/ pipeline (full video)
    kaggle_bdd100k — (stub) Kaggle BDD100K, not yet implemented

For the `hf_bdd100k` backend, codec simulation is performed image-by-image using
JPEG compression at a quality level derived from (qp_base, delta_qp_roi). This is a
coarse proxy for HEVC QP; use the `video_clips` backend with encode_hm.py for
paper-grade per-CTU RDO.

Run from repo root:
    python vcm/build_real_offline_profile.py \\
        --dataset_backend hf_bdd100k \\
        --hf_dataset_name dgural/bdd100k \\
        --max_samples 1000 \\
        --output_csv data/offline_profiles/bdd100k_hf_detection_profile.csv
"""

from __future__ import print_function

import argparse
import csv
import io
import json
import os
import sys
import time

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VCM = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO)
sys.path.insert(0, _VCM)

import vcm_config as cfg  # noqa: E402
from roi_task_extractor import summarize_from_boxes  # noqa: E402


CSV_FIELDS = [
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


# ---------------------------------------------------------------------------
# Codec simulation (image-level, JPEG proxy for HEVC QP)
# ---------------------------------------------------------------------------

def _qp_to_jpeg_quality(qp):
    """
    Approximate mapping: lower HEVC QP = higher quality = higher JPEG quality.
        QP=24 -> ~65, QP=28 -> ~55, QP=32 -> ~45, QP=36 -> ~35, QP=40 -> ~25
    """
    quality = int(65 - (qp - 24) * 2.5)
    return max(5, min(95, quality))


def _compress_image_jpeg(pil_img, quality):
    """Return in-memory JPEG-compressed PIL Image + bytes."""
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=quality)
    size_bytes = buf.tell()
    buf.seek(0)
    from PIL import Image  # noqa: WPS433
    return Image.open(buf).convert("RGB"), size_bytes


def _compress_roi_aware(pil_img, boxes, qp_base, delta_qp_roi):
    """
    JPEG compression with ROI bias:
    - background: compressed at qp_base quality
    - ROI regions: composited with higher quality patch (qp_base + delta_qp_roi)
    Returns (compressed PIL Image, total bytes, effective_bitrate_proxy).
    """
    from PIL import Image  # noqa: WPS433
    q_bg = _qp_to_jpeg_quality(qp_base)
    q_roi = _qp_to_jpeg_quality(max(0, qp_base + delta_qp_roi))  # delta<0 -> lower QP -> better

    bg, bg_bytes = _compress_image_jpeg(pil_img, q_bg)
    w, h = pil_img.size

    total_bytes = bg_bytes
    if delta_qp_roi < 0 and boxes:
        roi_img = bg.copy()
        for box in boxes:
            x1, y1, x2, y2 = (
                max(0, int(box[0])), max(0, int(box[1])),
                min(w, int(box[2])), min(h, int(box[3])),
            )
            if x2 <= x1 or y2 <= y1:
                continue
            patch = pil_img.crop((x1, y1, x2, y2))
            patch_compressed, patch_bytes = _compress_image_jpeg(patch, q_roi)
            roi_img.paste(patch_compressed, (x1, y1))
            total_bytes += patch_bytes

        buf = io.BytesIO()
        roi_img.save(buf, format="PNG")
        return roi_img, total_bytes, total_bytes
    return bg, bg_bytes, bg_bytes


def _estimate_bitrate(total_bytes, width, height, fps=30.0):
    """Rough per-frame bitrate proxy in Mbps (assuming 1 frame = 1/fps seconds)."""
    duration_s = 1.0 / fps
    return (total_bytes * 8.0 / 1e6) / duration_s


# ---------------------------------------------------------------------------
# mAP computation (reuse compute_map module)
# ---------------------------------------------------------------------------

def _yolo_on_pil(pil_img, model, conf, iou, imgsz):
    """Run YOLO on a PIL image, return standard det dict."""
    results = model.predict(source=pil_img, conf=conf, iou=iou, imgsz=imgsz, verbose=False)
    if not results or results[0].boxes is None or len(results[0].boxes) == 0:
        return {"boxes": [], "scores": [], "classes": []}
    res = results[0]
    return {
        "boxes": res.boxes.xyxy.cpu().numpy().tolist(),
        "scores": res.boxes.conf.cpu().numpy().tolist(),
        "classes": res.boxes.cls.cpu().numpy().astype(int).tolist(),
    }


def _map_for_image(pred, gt_boxes, gt_classes):
    """Compute mAP@[0.5:0.95] for a single image using compute_map module."""
    from vcm.profile.compute_map import per_frame_map  # noqa: WPS433  # vcm.profile, not vcm.data_backends
    gt_det = {"frame_id": 0, "boxes": gt_boxes, "scores": [1.0] * len(gt_boxes), "classes": gt_classes}
    pred_det = {"frame_id": 0, "boxes": pred["boxes"], "scores": pred["scores"], "classes": pred["classes"]}
    vals = per_frame_map([pred_det], [gt_det])
    return vals[0] if vals else 1.0


def _sample_network(rng):
    bw = float(np.clip(rng.uniform(1.5, 9.5), 0.1, cfg.MAX_BANDWIDTH_MBPS))
    rt = float(rng.uniform(25.0, 180.0))
    ls = float(np.clip(rng.uniform(0.0, 0.08), 0.0, 1.0))
    return bw, rt, ls


# ---------------------------------------------------------------------------
# HF BDD100K backend
# ---------------------------------------------------------------------------

def run_hf_bdd100k(args, csv_writer, rng):
    try:
        from vcm.data_backends.hf_bdd100k_loader import load_hf_bdd100k  # noqa: WPS433
    except Exception as e:
        raise SystemExit("Import error in hf_bdd100k_loader: %s: %s" % (type(e).__name__, e))

    debug_path = os.path.join(_REPO, "results", "vcm_logs", "hf_bdd100k_debug.json")
    image_dir = os.path.join(_REPO, "data", "bdd100k_hf", "images")

    samples = load_hf_bdd100k(
        hf_dataset_name=args.hf_dataset_name,
        split=args.hf_split,
        max_samples=args.max_samples,
        image_dir=image_dir,
        debug_path=debug_path,
    )

    print("Loading YOLO weights: %s" % args.yolo_weights)
    from ultralytics import YOLO  # noqa: WPS433
    model = YOLO(args.yolo_weights)

    total_rows = 0
    t_start = time.time()

    for i, sample in enumerate(samples):
        img_path = sample["image_path"]
        if not img_path or not os.path.isfile(img_path):
            continue

        from PIL import Image  # noqa: WPS433
        pil_orig = Image.open(img_path).convert("RGB")
        w, h = pil_orig.size

        gt_boxes = sample["boxes"]
        gt_classes = sample["classes"]

        roi_summary = summarize_from_boxes(
            boxes=gt_boxes,
            confidences=sample["confidences"] or [1.0] * len(gt_boxes),
            frame_width=w,
            frame_height=h,
            prev_boxes=None,
        )

        # YOLO on uncompressed (pseudo-GT if no GT boxes available)
        if not gt_boxes:
            uncompressed_det = _yolo_on_pil(pil_orig, model,
                                             args.yolo_conf, args.yolo_iou, args.yolo_imgsz)
            gt_boxes = uncompressed_det["boxes"]
            gt_classes = uncompressed_det["classes"]
            roi_summary = summarize_from_boxes(
                boxes=gt_boxes,
                confidences=uncompressed_det["scores"],
                frame_width=w,
                frame_height=h,
            )

        for qp_base in cfg.QP_BASE_SET:
            for delta_qp_roi in cfg.ROI_QP_OFFSET_SET:
                compressed_img, total_bytes, _ = _compress_roi_aware(
                    pil_orig, gt_boxes, qp_base, delta_qp_roi
                )
                bitrate_mbps = _estimate_bitrate(total_bytes, w, h, fps=30.0)

                pred = _yolo_on_pil(compressed_img, model,
                                     args.yolo_conf, args.yolo_iou, args.yolo_imgsz)

                u_task_gt = _map_for_image(pred, gt_boxes, gt_classes)
                bw, rt, ls = _sample_network(rng)

                row = {
                    "video_id": 0,
                    "frame_id": i,
                    "bandwidth_mbps": round(bw, 5),
                    "rtt_ms": round(rt, 3),
                    "loss": round(ls, 5),
                    "qp_base": int(qp_base),
                    "delta_qp_roi": int(delta_qp_roi),
                    "bitrate_mbps": round(min(bitrate_mbps, cfg.MAX_BITRATE_MBPS * 1.5), 5),
                    "roi_area": round(roi_summary["roi_area"], 5),
                    "obj_count": int(roi_summary["obj_count"]),
                    "mean_conf": round(roi_summary["mean_conf"], 5),
                    "motion": 0.0,   # no temporal sequence in image backend
                    "u_task_gt": round(float(u_task_gt), 5),
                }
                csv_writer.writerow(row)
                total_rows += 1

        if (i + 1) % 50 == 0:
            elapsed = time.time() - t_start
            print("  Processed %d / %d samples | rows=%d | %.1fs" % (
                i + 1, len(samples), total_rows, elapsed))

    print("  Total rows written: %d" % total_rows)


# ---------------------------------------------------------------------------
# Kaggle stub
# ---------------------------------------------------------------------------

def run_kaggle_bdd100k(args, csv_writer, rng):
    raise NotImplementedError(
        "kaggle_bdd100k backend not yet implemented. Use --dataset_backend hf_bdd100k."
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(
        description="Build CL-ROI-VCM real offline profile from various dataset backends."
    )
    ap.add_argument(
        "--dataset_backend",
        choices=["hf_bdd100k", "video_clips", "kaggle_bdd100k"],
        default="hf_bdd100k",
        help="Data source backend.",
    )
    # HF options
    ap.add_argument("--hf_dataset_name", type=str, default="dgural/bdd100k")
    ap.add_argument("--hf_split", type=str, default="train")
    ap.add_argument("--max_samples", type=int, default=1000,
                    help="Max images to process (0 = all).")
    # YOLO options
    ap.add_argument("--yolo_weights", type=str, default="yolov8m.pt")
    ap.add_argument("--yolo_conf", type=float, default=0.25)
    ap.add_argument("--yolo_iou", type=float, default=0.5)
    ap.add_argument("--yolo_imgsz", type=int, default=1280)
    ap.add_argument("--yolo_device", type=str, default="")
    # Output
    ap.add_argument(
        "--output_csv",
        type=str,
        default=os.path.join(_REPO, "data", "offline_profiles",
                             "bdd100k_hf_detection_profile.csv"),
    )
    ap.add_argument("--seed", type=int, default=42)
    return ap.parse_args()


def main():
    args = parse_args()
    rng = np.random.RandomState(args.seed)

    os.makedirs(os.path.dirname(args.output_csv), exist_ok=True)
    os.makedirs(os.path.join(_REPO, "results", "vcm_logs"), exist_ok=True)

    print("Backend: %s" % args.dataset_backend)
    print("Output:  %s" % args.output_csv)

    with open(args.output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        if args.dataset_backend == "hf_bdd100k":
            run_hf_bdd100k(args, writer, rng)
        elif args.dataset_backend == "video_clips":
            raise SystemExit(
                "For video_clips backend use: python -m vcm.profile.build_real_profile"
            )
        elif args.dataset_backend == "kaggle_bdd100k":
            run_kaggle_bdd100k(args, writer, rng)

    print("Done -> %s" % args.output_csv)


if __name__ == "__main__":
    main()
