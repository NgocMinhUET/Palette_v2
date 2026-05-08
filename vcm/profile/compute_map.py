# coding=utf-8
"""
Per-frame mAP computation between detected (decoded) and pseudo-GT (uncompressed) YOLO outputs.

We use a lightweight per-frame implementation of mAP@[0.5:0.95] over IoU
thresholds 0.5..0.95 step 0.05, averaged across YOLO classes present in either
prediction or pseudo-GT.

A per-frame scalar `u_task_gt` ∈ [0, 1] is what the controller cares about; the
exact metric definition is configurable but defaults to mAP@[0.5:0.95].
"""

from __future__ import print_function

import numpy as np


IOU_THRS = np.linspace(0.5, 0.95, 10)


def _iou_matrix(boxes_a, boxes_b):
    if len(boxes_a) == 0 or len(boxes_b) == 0:
        return np.zeros((len(boxes_a), len(boxes_b)), dtype=np.float64)
    a = np.asarray(boxes_a, dtype=np.float64)
    b = np.asarray(boxes_b, dtype=np.float64)
    ax1, ay1, ax2, ay2 = a[:, 0:1], a[:, 1:2], a[:, 2:3], a[:, 3:4]
    bx1, by1, bx2, by2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    inter_x1 = np.maximum(ax1, bx1)
    inter_y1 = np.maximum(ay1, by1)
    inter_x2 = np.minimum(ax2, bx2)
    inter_y2 = np.minimum(ay2, by2)
    iw = np.clip(inter_x2 - inter_x1, 0.0, None)
    ih = np.clip(inter_y2 - inter_y1, 0.0, None)
    inter = iw * ih
    area_a = np.clip(ax2 - ax1, 0.0, None) * np.clip(ay2 - ay1, 0.0, None)
    area_b = np.clip(bx2 - bx1, 0.0, None) * np.clip(by2 - by1, 0.0, None)
    union = area_a + area_b - inter + 1e-9
    return inter / union


def _ap_one_class_one_threshold(pred_boxes, pred_scores, gt_boxes, iou_thr):
    """11-point interpolated AP at a single IoU threshold."""
    if len(gt_boxes) == 0:
        if len(pred_boxes) == 0:
            return 1.0
        return 0.0
    if len(pred_boxes) == 0:
        return 0.0

    order = np.argsort(-np.asarray(pred_scores))
    pred_boxes = [pred_boxes[i] for i in order]
    pred_scores = [pred_scores[i] for i in order]
    iou = _iou_matrix(pred_boxes, gt_boxes)

    matched_gt = np.zeros(len(gt_boxes), dtype=bool)
    tp = np.zeros(len(pred_boxes))
    fp = np.zeros(len(pred_boxes))
    for i in range(len(pred_boxes)):
        best_j = -1
        best_iou = 0.0
        for j in range(len(gt_boxes)):
            if matched_gt[j]:
                continue
            if iou[i, j] >= iou_thr and iou[i, j] > best_iou:
                best_iou = iou[i, j]
                best_j = j
        if best_j >= 0:
            tp[i] = 1
            matched_gt[best_j] = True
        else:
            fp[i] = 1

    tp_cum = np.cumsum(tp)
    fp_cum = np.cumsum(fp)
    recall = tp_cum / float(len(gt_boxes))
    precision = tp_cum / np.maximum(tp_cum + fp_cum, 1e-9)

    ap = 0.0
    for r in np.linspace(0, 1, 11):
        mask = recall >= r
        p = precision[mask].max() if np.any(mask) else 0.0
        ap += p / 11.0
    return float(ap)


def per_frame_map(pred_dets, gt_dets):
    """
    Args:
        pred_dets: list of dict {boxes, scores, classes, frame_id}
        gt_dets:   list of dict {boxes, scores, classes, frame_id}
                   Both lists are aligned by frame_id (same length).
    Returns:
        list[float] u_task_gt per frame (length = min len) in [0, 1].
    """
    n = min(len(pred_dets), len(gt_dets))
    out = []
    for i in range(n):
        p = pred_dets[i]
        g = gt_dets[i]
        cls_set = sorted(set(list(p["classes"]) + list(g["classes"])))
        if not cls_set:
            out.append(1.0)
            continue
        ap_per_cls = []
        for c in cls_set:
            p_idx = [k for k, x in enumerate(p["classes"]) if x == c]
            g_idx = [k for k, x in enumerate(g["classes"]) if x == c]
            p_boxes = [p["boxes"][k] for k in p_idx]
            p_scores = [p["scores"][k] for k in p_idx]
            g_boxes = [g["boxes"][k] for k in g_idx]
            ap_iou = [
                _ap_one_class_one_threshold(p_boxes, p_scores, g_boxes, t) for t in IOU_THRS
            ]
            ap_per_cls.append(float(np.mean(ap_iou)))
        u = float(np.clip(np.mean(ap_per_cls), 0.0, 1.0))
        out.append(u)
    return out
