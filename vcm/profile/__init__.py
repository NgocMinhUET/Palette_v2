# coding=utf-8
"""
CL-ROI-VCM real offline profile pipeline.

Modules:
    paths              — central path/CLI/env configuration
    extract_frames     — video -> PNG frames (uncompressed)
    run_yolo           — Ultralytics YOLOv8 inference utility
    encode_x265        — fast iteration encoder (frame-level ROI-biased QP)
    encode_hm          — HM Reference encoder (per-CTU QP delta) [stub]
    compute_map        — torchmetrics mAP per frame (det vs pseudo-GT)
    build_real_profile — orchestrator producing real_*_profile.csv

The CSV schema produced is identical to dummy_vcm_profile.csv so the existing
training/evaluation pipeline (train_vcm.py, test_vcm.py, evaluate_vcm.py) is unchanged.
"""
