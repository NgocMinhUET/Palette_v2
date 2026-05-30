# coding=utf-8
"""
Ultralytics YOLOv8 inference utilities.

Two roles in the pipeline:
1) On UNCOMPRESSED frames (extract_frames output) -> pseudo-GT bboxes for mAP.
   This avoids dataset-specific GT format wrangling and is the standard practice
   in VCM (CompressAI-Vision, JVET CTC for Machines).
2) On DECODED frames (after encode-decode) -> "predicted" bboxes for u_task_gt.
"""

from __future__ import print_function

import argparse
import glob
import json
import os


def _lazy_yolo(weights):
    """Import + load Ultralytics YOLO (heavy)."""
    from ultralytics import YOLO  # noqa: WPS433
    return YOLO(weights)


def run_on_dir(frames_dir, weights, conf=0.25, iou=0.5, device=None, imgsz=1280, stream=True):
    """
    Run YOLOv8 on all PNGs in `frames_dir`.

    stream=True (default): one frame at a time — avoids CUDA OOM when folders
    have 100+ frames at imgsz=1280 (build_real_profile decoded dirs).

    Returns: list of dict per frame (sorted by filename)
        {
          "frame_path": str,
          "frame_id": int,                # parsed from filename "00000123.png"
          "boxes": [[x1,y1,x2,y2], ...],
          "scores": [...],
          "classes": [...],
        }
    """
    model = _lazy_yolo(weights)
    files = sorted(glob.glob(os.path.join(frames_dir, "*.png")))
    if not files:
        return []

    predict_kwargs = dict(conf=conf, iou=iou, imgsz=imgsz, verbose=False, stream=stream)
    if device is not None:
        predict_kwargs["device"] = device

    out = []
    results = model.predict(source=files, **predict_kwargs)
    for path, res in zip(files, results):
        fid = int(os.path.splitext(os.path.basename(path))[0])
        if res.boxes is None or len(res.boxes) == 0:
            out.append({"frame_path": path, "frame_id": fid, "boxes": [], "scores": [], "classes": []})
            continue
        xyxy = res.boxes.xyxy.cpu().numpy().tolist()
        scores = res.boxes.conf.cpu().numpy().tolist()
        cls = res.boxes.cls.cpu().numpy().astype(int).tolist()
        out.append({
            "frame_path": path,
            "frame_id": fid,
            "boxes": xyxy,
            "scores": scores,
            "classes": cls,
        })
    return out


def save_detections_json(detections, json_path):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w") as f:
        json.dump(detections, f)


def parse_args():
    ap = argparse.ArgumentParser(description="Standalone YOLO inference on a frames folder.")
    ap.add_argument("--frames_dir", type=str, required=True)
    ap.add_argument("--out_json", type=str, required=True)
    ap.add_argument("--weights", type=str, default="yolov8m.pt")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--device", type=str, default="")
    ap.add_argument("--imgsz", type=int, default=1280)
    return ap.parse_args()


def main():
    args = parse_args()
    dets = run_on_dir(
        args.frames_dir,
        args.weights,
        conf=args.conf,
        iou=args.iou,
        device=args.device or None,
        imgsz=args.imgsz,
    )
    save_detections_json(dets, args.out_json)
    print("Wrote %d frame detections to %s" % (len(dets), args.out_json))


if __name__ == "__main__":
    main()
