# coding=utf-8
"""
Hugging Face BDD100K loader for CL-ROI-VCM offline profile.

Dataset: dgural/bdd100k  (https://huggingface.co/datasets/dgural/bdd100k)

Outputs a list of standardised sample dicts:
    {
        "frame_id":   int,
        "image_path": str,          # exported PNG in local cache
        "width":      int,
        "height":     int,
        "boxes":      [[x1,y1,x2,y2], ...],   # pixel coords
        "classes":    [int, ...],              # class indices
        "confidences": [float, ...],           # always 1.0 for GT
        "weather":    str | None,
        "timeofday":  str | None,
        "scene":      str | None,
    }

LIMITATION: This backend uses image/keyframe detection data only.
Temporal tracking and motion estimation require full video sequences;
use the vcm/profile/ pipeline (video clips) for that.
"""

from __future__ import print_function

import json
import os

import numpy as np


# Canonical BDD100K category list (same order as official detection task)
BDD_CATEGORIES = [
    "pedestrian", "rider", "car", "truck", "bus",
    "train", "motorcycle", "bicycle", "traffic light", "traffic sign",
]
BDD_CAT_TO_IDX = {c: i for i, c in enumerate(BDD_CATEGORIES)}


def _export_image(pil_img, path):
    """Save PIL image to path (PNG)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pil_img.save(path, format="PNG")


def convert_hf_bdd_sample_to_standard(sample, frame_id, image_dir):
    """
    Convert a single HuggingFace dgural/bdd100k sample to the pipeline standard format.

    Tries several known field layouts. If none match, returns a valid stub
    with empty boxes so the pipeline does not crash.

    Returns standard dict (see module docstring).
    """
    # --- save image ---
    img = sample.get("image") or sample.get("img") or sample.get("pixel_values")
    if img is None:
        # Try to find any PIL-like field
        for v in sample.values():
            try:
                if hasattr(v, "save"):
                    img = v
                    break
            except Exception:
                pass

    w, h = (1280, 720)
    img_path = os.path.join(image_dir, "%08d.png" % frame_id)
    if img is not None:
        try:
            if not hasattr(img, "save"):
                from PIL import Image  # noqa: WPS433
                img = Image.fromarray(np.array(img))
            _export_image(img, img_path)
            w, h = img.size  # PIL: (width, height)
        except Exception as e:
            print("    WARN: could not save image for frame %d: %s" % (frame_id, e))
    else:
        img_path = ""

    # --- parse annotations ---
    boxes, classes, confidences = [], [], []

    # Layout A: "labels" field (official BDD100K HF format)
    labels = sample.get("labels") or sample.get("annotations") or []
    if isinstance(labels, (list, tuple)) and len(labels) > 0:
        first = labels[0]
        # Layout A1: list of dicts with "box2d" and "category"
        if isinstance(first, dict) and "box2d" in first:
            for lbl in labels:
                b2d = lbl.get("box2d", {})
                x1 = float(b2d.get("x1", b2d.get("xmin", 0)))
                y1 = float(b2d.get("y1", b2d.get("ymin", 0)))
                x2 = float(b2d.get("x2", b2d.get("xmax", 0)))
                y2 = float(b2d.get("y2", b2d.get("ymax", 0)))
                cat = str(lbl.get("category", lbl.get("class", "car"))).lower()
                cidx = BDD_CAT_TO_IDX.get(cat, 2)  # default car=2
                boxes.append([x1, y1, x2, y2])
                classes.append(cidx)
                confidences.append(1.0)
        # Layout A2: list of dicts with COCO-style "bbox" [x,y,w,h]
        elif isinstance(first, dict) and "bbox" in first:
            for lbl in labels:
                bx, by, bw, bh = lbl["bbox"]
                boxes.append([float(bx), float(by), float(bx + bw), float(by + bh)])
                cat = str(lbl.get("category_id", lbl.get("category", 2)))
                classes.append(int(cat) if cat.isdigit() else 2)
                confidences.append(1.0)

    # Layout B: separate "objects" field (Roboflow-style HF exports)
    if not boxes:
        objects = sample.get("objects") or {}
        if isinstance(objects, dict):
            bboxes_raw = (
                objects.get("bbox")
                or objects.get("boxes")
                or objects.get("bounding_box")
                or []
            )
            cats_raw = objects.get("category") or objects.get("label") or objects.get("class_id") or []
            for i, bb in enumerate(bboxes_raw):
                if len(bb) == 4:
                    # Could be [x,y,w,h] or [x1,y1,x2,y2] — normalised or pixel
                    x1, y1, x2, y2 = bb
                    # If normalised (values in [0,1])
                    if max(x1, y1, x2, y2) <= 1.0 and min(x1, y1, x2, y2) >= 0.0:
                        x1, y1, x2, y2 = x1 * w, y1 * h, x2 * w, y2 * h
                    boxes.append([float(x1), float(y1), float(x2), float(y2)])
                    c = cats_raw[i] if i < len(cats_raw) else 2
                    classes.append(int(c))
                    confidences.append(1.0)

    # --- metadata ---
    attrs = sample.get("attributes") or {}
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}

    return {
        "frame_id": frame_id,
        "image_path": img_path,
        "width": w,
        "height": h,
        "boxes": boxes,
        "classes": classes,
        "confidences": confidences,
        "weather": attrs.get("weather") if isinstance(attrs, dict) else None,
        "timeofday": attrs.get("timeofday") if isinstance(attrs, dict) else None,
        "scene": attrs.get("scene") if isinstance(attrs, dict) else None,
    }


def _dump_debug(samples_raw, debug_path):
    """Persist first 3 raw samples as JSON for inspection (safe serialisation)."""
    os.makedirs(os.path.dirname(debug_path), exist_ok=True)
    out = []
    for s in samples_raw[:3]:
        row = {}
        for k, v in s.items():
            try:
                json.dumps(v)
                row[k] = v
            except (TypeError, ValueError):
                row[k] = "<non-serialisable: %s>" % type(v).__name__
        out.append(row)
    with open(debug_path, "w") as f:
        json.dump(out, f, indent=2)
    print("  Debug dump -> %s" % debug_path)


def load_hf_bdd100k(
    hf_dataset_name="dgural/bdd100k",
    split="train",
    max_samples=1000,
    image_dir="data/bdd100k_hf/images",
    debug_path="results/vcm_logs/hf_bdd100k_debug.json",
):
    """
    Load BDD100K from HuggingFace and return list of standardised sample dicts.

    Args:
        hf_dataset_name: HF repo id.
        split: dataset split (train / validation / test).
        max_samples: cap on number of samples (0 = all).
        image_dir: local dir to export PNG images.
        debug_path: JSON debug file for first 3 raw samples.

    Returns:
        list[dict] of standard samples.
    """
    try:
        from datasets import load_dataset  # noqa: WPS433
    except ImportError as e:
        raise SystemExit(
            "Could not import 'datasets': %s\n"
            "Make sure you are in the vcm env: conda activate vcm\n"
            "Then: pip install datasets pillow" % e
        )

    print("Loading %s (split=%s, max_samples=%s) from HuggingFace ..." % (
        hf_dataset_name, split, max_samples if max_samples else "all"
    ))
    try:
        ds = load_dataset(hf_dataset_name, split=split, trust_remote_code=True)
    except TypeError:
        # Older datasets versions do not support trust_remote_code
        ds = load_dataset(hf_dataset_name, split=split)

    if max_samples and max_samples > 0:
        ds = ds.select(range(min(max_samples, len(ds))))

    # Print available fields once
    sample0 = ds[0]
    print("  Available keys: %s" % sorted(sample0.keys()))
    _dump_debug([ds[i] for i in range(min(3, len(ds)))], debug_path)

    os.makedirs(image_dir, exist_ok=True)
    results = []
    for i in range(len(ds)):
        sample = ds[i]
        try:
            std = convert_hf_bdd_sample_to_standard(sample, frame_id=i, image_dir=image_dir)
        except Exception as e:
            print("  WARN: frame %d conversion failed: %s" % (i, e))
            std = {
                "frame_id": i, "image_path": "", "width": 1280, "height": 720,
                "boxes": [], "classes": [], "confidences": [],
                "weather": None, "timeofday": None, "scene": None,
            }
        results.append(std)
        if (i + 1) % 100 == 0:
            print("  Loaded %d / %d samples" % (i + 1, len(ds)))

    print("  Total samples loaded: %d" % len(results))
    return results
