# coding=utf-8
"""
End-to-end orchestrator: real BDD videos -> CSV profile compatible with
`vcm/vcm_env.py` and `vcm/train_vcm.py`.

Pipeline per clip:
    extract_frames -> YUV + PNG
    YOLO on uncompressed PNG  -> pseudo-GT bboxes  (per-frame ROI summary)
    for each (qp_base, delta_qp_roi) in QP_BASE_SET x ROI_QP_OFFSET_SET:
        encode (x265 fast / HM rigorous) -> bitstream + decoded PNG
        YOLO on decoded PNG               -> predicted bboxes
        per-frame mAP(pred vs pseudo-GT)  -> u_task_gt
        sample (bandwidth, rtt, loss) per-frame from a synthetic distribution
        write CSV row
"""

from __future__ import print_function

import argparse
import csv
import json
import math
import os
import sys
import time

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_VCM_DIR = os.path.dirname(_THIS_DIR)
_REPO = os.path.dirname(_VCM_DIR)
sys.path.insert(0, _REPO)
sys.path.insert(0, _VCM_DIR)

import vcm_config as cfg  # noqa: E402
from roi_task_extractor import summarize_from_boxes  # noqa: E402

from . import paths as P  # noqa: E402
from . import extract_frames as ef  # noqa: E402
from . import run_yolo as yolo  # noqa: E402
from . import encode_x265 as enc_x265  # noqa: E402
from . import compute_map as cmap  # noqa: E402


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


def _sample_network(rng):
    bw = float(np.clip(rng.uniform(1.5, 9.5), 0.1, cfg.MAX_BANDWIDTH_MBPS))
    rt = float(rng.uniform(25.0, 180.0))
    ls = float(np.clip(rng.uniform(0.0, 0.08), 0.0, 1.0))
    return bw, rt, ls


def _summarize_per_frame(gt_dets, width, height):
    """ROI summary per frame using uncompressed YOLO outputs."""
    summaries = []
    prev_boxes = None
    for d in gt_dets:
        s = summarize_from_boxes(
            boxes=d["boxes"],
            confidences=d["scores"],
            frame_width=width,
            frame_height=height,
            prev_boxes=prev_boxes,
        )
        summaries.append(s)
        prev_boxes = d["boxes"] if len(d["boxes"]) > 0 else prev_boxes
    return summaries


def _process_clip(meta, video_id, args, csv_writer, rng):
    name = meta["name"]
    width, height = int(meta["width"]), int(meta["height"])
    nb = int(meta["nb_frames"])
    print("[clip %d] %s (%dx%d, %d frames)" % (video_id, name, width, height, nb))

    print("  YOLO on uncompressed (pseudo-GT) ...")
    t0 = time.time()
    gt_dets = yolo.run_on_dir(
        meta["frames_dir"], args.yolo_weights,
        conf=args.yolo_conf, iou=args.yolo_iou,
        device=(args.yolo_device or None), imgsz=args.yolo_imgsz,
    )
    print("    %d frames in %.1fs" % (len(gt_dets), time.time() - t0))
    if len(gt_dets) != nb:
        print("    WARN: gt frames %d != nb %d, using min." % (len(gt_dets), nb))
    nb = min(nb, len(gt_dets))
    gt_dets = gt_dets[:nb]

    summaries = _summarize_per_frame(gt_dets, width, height)
    has_roi_per_frame = [(s["obj_count"] > 0 and s["roi_area"] > 0.005) for s in summaries]

    x265_root = os.path.join(args.workdir, name, "x265")
    os.makedirs(x265_root, exist_ok=True)

    for qp_base in cfg.QP_BASE_SET:
        for delta_qp_roi in cfg.ROI_QP_OFFSET_SET:
            tag = "qp%d_d%+d" % (qp_base, delta_qp_roi)
            print("  encode/decode/det %s ..." % tag)
            t0 = time.time()
            enc = enc_x265.encode_decode_x265(
                meta=meta,
                qp_base=qp_base,
                delta_qp_roi=delta_qp_roi,
                has_roi_per_frame=has_roi_per_frame,
                out_root=x265_root,
                x265_bin=args.x265_bin,
                ffmpeg_bin=args.ffmpeg_bin,
            )
            pred_dets = yolo.run_on_dir(
                enc["decoded_dir"], args.yolo_weights,
                conf=args.yolo_conf, iou=args.yolo_iou,
                device=(args.yolo_device or None), imgsz=args.yolo_imgsz,
            )
            pred_dets = pred_dets[:nb]
            u_task = cmap.per_frame_map(pred_dets, gt_dets)
            print("    %s: bitrate=%.2f Mbps, mean u_task_gt=%.3f, %.1fs" %
                  (tag, enc["bitrate_mbps_avg"], float(np.mean(u_task)) if u_task else 0.0,
                   time.time() - t0))

            per_frame_br = enc["per_frame_bitrate_mbps"]
            for f in range(nb):
                bw, rt, ls = _sample_network(rng)
                row = {
                    "video_id": video_id,
                    "frame_id": f,
                    "bandwidth_mbps": round(bw, 5),
                    "rtt_ms": round(rt, 3),
                    "loss": round(ls, 5),
                    "qp_base": int(qp_base),
                    "delta_qp_roi": int(delta_qp_roi),
                    "bitrate_mbps": round(per_frame_br[f] if f < len(per_frame_br) else enc["bitrate_mbps_avg"], 5),
                    "roi_area": round(summaries[f]["roi_area"], 5),
                    "obj_count": int(summaries[f]["obj_count"]),
                    "mean_conf": round(summaries[f]["mean_conf"], 5),
                    "motion": round(min(summaries[f]["motion"], cfg.MAX_MOTION), 5),
                    "u_task_gt": round(u_task[f] if f < len(u_task) else 0.0, 5),
                }
                csv_writer.writerow(row)


def parse_args():
    ap = argparse.ArgumentParser(description="Build real CL-ROI-VCM offline profile from BDD videos.")
    P.add_common_args(ap)
    ap.add_argument("--encoder", choices=["x265", "hm"], default="x265")
    ap.add_argument("--max_seconds", type=int, default=5)
    ap.add_argument("--target_w", type=int, default=1280)
    ap.add_argument("--target_h", type=int, default=720)
    ap.add_argument("--limit_clips", type=int, default=20)
    ap.add_argument("--yolo_conf", type=float, default=0.25)
    ap.add_argument("--yolo_iou", type=float, default=0.5)
    ap.add_argument("--yolo_device", type=str, default="")
    ap.add_argument("--yolo_imgsz", type=int, default=1280)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip_extract", action="store_true",
                    help="Reuse workdir/<name>/ if already extracted.")
    return ap.parse_args()


def main():
    args = parse_args()
    if args.encoder == "hm":
        raise SystemExit("HM encoder is a stub. Use --encoder x265 for now.")
    if not args.bdd_videos_dir:
        raise SystemExit("--bdd_videos_dir or BDD_VIDEOS_DIR is required.")
    P.ensure_dirs(args.workdir, os.path.dirname(args.out_csv))

    rng = np.random.RandomState(args.seed)
    videos = ef.list_videos(args.bdd_videos_dir)
    if args.limit_clips and args.limit_clips > 0:
        videos = videos[: args.limit_clips]
    print("Found %d video clips." % len(videos))

    target_size = (args.target_w, args.target_h)
    max_seconds = args.max_seconds if args.max_seconds > 0 else None

    metas = []
    for v in videos:
        clip_dir = os.path.join(args.workdir, os.path.splitext(os.path.basename(v))[0])
        meta_path = os.path.join(clip_dir, "meta.json")
        if args.skip_extract and os.path.isfile(meta_path):
            with open(meta_path) as f:
                metas.append(json.load(f))
            continue
        meta = ef.extract_one(
            v, args.workdir, args.ffmpeg_bin,
            max_seconds=max_seconds, target_size=target_size,
        )
        metas.append(meta)

    print("Writing %s ..." % args.out_csv)
    with open(args.out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for vid, meta in enumerate(metas):
            _process_clip(meta, vid, args, writer, rng)
    print("Done. Profile -> %s" % args.out_csv)


if __name__ == "__main__":
    main()
