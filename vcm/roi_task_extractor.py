# coding=utf-8
"""
ROI / detection summarization for task-aware state.

Pure geometry + statistics — no online mAP.
"""

import numpy as np


def summarize_from_boxes(boxes, confidences, frame_width, frame_height, prev_boxes=None):
    """
    Normalize ROI/task summary from bounding boxes.

    Args:
        boxes: list of [x1,y1,x2,y2] in pixels, or None / empty for dummy mode.
        confidences: list of floats same length as boxes.
        frame_width, frame_height: frame size (> 0).
        prev_boxes: previous frame boxes for motion proxy (same structure).

    Returns:
        dict with roi_area, obj_count, mean_conf, motion.
    """
    fw = float(max(frame_width, 1))
    fh = float(max(frame_height, 1))
    area_frame = fw * fh

    if not boxes or len(boxes) == 0:
        rng = np.random.RandomState(0)
        obj_count = int(rng.randint(1, 6))
        mean_conf = float(rng.uniform(0.45, 0.85))
        roi_area = float(np.clip(obj_count * rng.uniform(0.02, 0.08), 0.0, 1.0))
        motion = float(rng.uniform(0.0, 15.0))
        return {
            "roi_area": roi_area,
            "obj_count": obj_count,
            "mean_conf": mean_conf,
            "motion": motion,
        }

    boxes = np.asarray(boxes, dtype=np.float64)
    confidences = np.asarray(confidences, dtype=np.float64)
    if confidences.size != boxes.shape[0]:
        confidences = np.ones(boxes.shape[0]) * 0.5

    obj_count = int(boxes.shape[0])
    areas = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0.0, boxes[:, 3] - boxes[:, 1])
    roi_area = float(np.clip(np.sum(areas) / area_frame, 0.0, 1.0))
    mean_conf = float(np.clip(np.mean(confidences), 0.0, 1.0))

    centers = np.stack([(boxes[:, 0] + boxes[:, 2]) * 0.5, (boxes[:, 1] + boxes[:, 3]) * 0.5], axis=1)
    motion = 0.0
    if prev_boxes is not None and len(prev_boxes) > 0:
        pb = np.asarray(prev_boxes, dtype=np.float64)
        pc = np.stack([(pb[:, 0] + pb[:, 2]) * 0.5, (pb[:, 1] + pb[:, 3]) * 0.5], axis=1)
        n = min(len(centers), len(pc))
        if n > 0:
            disp = np.linalg.norm(centers[:n] - pc[:n], axis=1)
            motion = float(np.mean(disp))

    return {
        "roi_area": roi_area,
        "obj_count": obj_count,
        "mean_conf": mean_conf,
        "motion": motion,
    }
