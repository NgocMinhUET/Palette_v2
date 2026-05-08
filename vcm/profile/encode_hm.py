# coding=utf-8
"""
HM (HEVC Test Model) encoder wrapper for *paper-grade* per-CTU QP delta.

Status: STUB. Full integration is enabled only after the x265 iteration loop
is validated end-to-end. The signature mirrors encode_x265.encode_decode_x265
so the orchestrator can swap encoders by --encoder {x265|hm}.

Per-CTU QP map mechanism (when fully enabled):
    HM accepts QP delta per-CTU through the --MaxCUWidth and --MaxCUHeight
    options combined with --MaxDeltaQP and an external CTU-level QP modulation
    file (via --dQPFile or --DeltaQpRD configuration in custom HM forks).

Pseudocode:
    1. tile frame into CTUs (default 64x64).
    2. for each CTU: if any GT box overlaps -> qp_offset = delta_qp_roi else 0.
    3. write per-frame, per-CTU QP delta map.
    4. invoke TAppEncoderStatic with cfg + qpfile.
    5. invoke TAppDecoderStatic to recover YUV.
    6. ffmpeg YUV -> PNG (same as x265 path).

This file currently raises NotImplementedError so the orchestrator falls back
to x265. Implementation will follow validated x265 results.
"""

from __future__ import print_function

import os
import subprocess


def _build_qp_delta_map(boxes_per_frame, width, height, ctu_size=64, delta_qp_roi=0):
    """
    Build per-frame, per-CTU QP delta map.

    Returns:
        list[ list[int] ] outer=frames, inner=CTU row-major.
    """
    n_ctu_x = (width + ctu_size - 1) // ctu_size
    n_ctu_y = (height + ctu_size - 1) // ctu_size
    maps = []
    for boxes in boxes_per_frame:
        cells = [0] * (n_ctu_x * n_ctu_y)
        for b in boxes:
            x1, y1, x2, y2 = b
            cx1, cy1 = int(x1 // ctu_size), int(y1 // ctu_size)
            cx2, cy2 = int(x2 // ctu_size), int(y2 // ctu_size)
            for cy in range(max(0, cy1), min(n_ctu_y - 1, cy2) + 1):
                for cx in range(max(0, cx1), min(n_ctu_x - 1, cx2) + 1):
                    cells[cy * n_ctu_x + cx] = int(delta_qp_roi)
        maps.append(cells)
    return maps


def encode_decode_hm(
    meta,
    qp_base,
    delta_qp_roi,
    boxes_per_frame,
    out_root,
    hm_encoder_bin,
    hm_decoder_bin,
    hm_cfg_dir,
    ffmpeg_bin="ffmpeg",
):
    """STUB — to be enabled after x265 path is validated. See module docstring."""
    if not hm_encoder_bin or not os.path.isfile(hm_encoder_bin):
        raise NotImplementedError(
            "HM path not configured. Set --hm_encoder_bin / --hm_decoder_bin / --hm_cfg_dir, "
            "or use --encoder x265 for the iteration loop."
        )
    raise NotImplementedError(
        "encode_decode_hm() is a stub. Full HM integration follows successful x265 validation."
    )


__all__ = ["encode_decode_hm", "_build_qp_delta_map"]
