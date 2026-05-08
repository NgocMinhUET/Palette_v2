# coding=utf-8
"""
Central path/configuration utilities for the real-profile pipeline.

All paths are configurable via CLI flags or environment variables so the pipeline
runs unmodified across machines.

Environment variables (all optional; CLI flags override these):
    BDD_VIDEOS_DIR      Directory containing input mp4/mov clips
    YOLO_WEIGHTS        Path to ultralytics YOLO weights (default yolov8m.pt)
    X265_BIN            x265 binary path (default 'x265' in PATH)
    FFMPEG_BIN          ffmpeg binary path (default 'ffmpeg' in PATH)
    HM_ENCODER_BIN      HM TAppEncoderStatic path
    HM_DECODER_BIN      HM TAppDecoderStatic path
    HM_CFG_DIR          HM cfg/ directory (encoder_lowdelay_P_main.cfg, etc.)
    PROFILE_WORKDIR     Scratch dir for intermediate YUV/bitstream files
    PROFILE_OUT_CSV     Output CSV path
"""

from __future__ import print_function

import argparse
import os


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env_default(env_var, fallback):
    value = os.environ.get(env_var, "")
    return value if value else fallback


def add_common_args(ap):
    """Add path-related flags to an argparse parser."""
    ap.add_argument(
        "--bdd_videos_dir",
        type=str,
        default=_env_default("BDD_VIDEOS_DIR", ""),
        help="Folder containing input mp4/mov clips (BDD subset).",
    )
    ap.add_argument(
        "--yolo_weights",
        type=str,
        default=_env_default("YOLO_WEIGHTS", "yolov8m.pt"),
        help="Ultralytics YOLOv8 weights (autodownloaded by ultralytics if name only).",
    )
    ap.add_argument(
        "--x265_bin",
        type=str,
        default=_env_default("X265_BIN", "x265"),
        help="x265 binary (must support --qp and --zones).",
    )
    ap.add_argument(
        "--ffmpeg_bin",
        type=str,
        default=_env_default("FFMPEG_BIN", "ffmpeg"),
        help="ffmpeg binary for YUV<->PNG conversion.",
    )
    ap.add_argument(
        "--hm_encoder_bin",
        type=str,
        default=_env_default("HM_ENCODER_BIN", ""),
        help="HM TAppEncoderStatic path (Phase 2B stub; required for --encoder hm).",
    )
    ap.add_argument(
        "--hm_decoder_bin",
        type=str,
        default=_env_default("HM_DECODER_BIN", ""),
        help="HM TAppDecoderStatic path.",
    )
    ap.add_argument(
        "--hm_cfg_dir",
        type=str,
        default=_env_default("HM_CFG_DIR", ""),
        help="HM cfg/ directory (encoder_lowdelay_P_main.cfg).",
    )
    ap.add_argument(
        "--workdir",
        type=str,
        default=_env_default("PROFILE_WORKDIR", os.path.join(REPO_ROOT, "data", "profile_work")),
        help="Scratch dir for intermediate YUV/bitstreams.",
    )
    ap.add_argument(
        "--out_csv",
        type=str,
        default=_env_default(
            "PROFILE_OUT_CSV",
            os.path.join(REPO_ROOT, "data", "offline_profiles", "real_bdd_profile.csv"),
        ),
        help="Output profile CSV.",
    )
    return ap


def ensure_dirs(*dirs):
    for d in dirs:
        if d:
            os.makedirs(d, exist_ok=True)


def resolve_repo_path(p):
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(REPO_ROOT, p))
