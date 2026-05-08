# coding=utf-8
"""
x265-based encoder wrapper for fast iteration of the VCM offline profile.

ROI handling note (frame-level approximation):
    libx265 does not accept per-CTU QP delta out-of-the-box like HM. For the
    fast iteration path we use a *frame-level ROI-biased QP*:

        effective_qp = qp_base + delta_qp_roi   if frame has any ROI (obj_count > 0)
                       qp_base                  otherwise

    This is a coarse approximation; the rigorous per-CTU QP map is implemented
    by encode_hm.py (HEVC Reference Software).

Outputs:
    workdir/<name>/x265/<qp_base>_<delta_qp_roi>/
        bitstream.265
        decoded.yuv
        decoded_frames/00000000.png ... (regenerated for YOLO)
        bitrate.json   { "bytes": int, "bitrate_mbps": float }
"""

from __future__ import print_function

import json
import os
import subprocess


def _per_frame_bytes(num_frames, total_bytes):
    if num_frames <= 0:
        return [0] * num_frames
    avg = total_bytes / float(num_frames)
    return [avg] * num_frames


def encode_decode_x265(
    meta,
    qp_base,
    delta_qp_roi,
    has_roi_per_frame,
    out_root,
    x265_bin="x265",
    ffmpeg_bin="ffmpeg",
):
    """
    Args:
        meta: dict from extract_frames.extract_one (yuv_path, width, height, fps, nb_frames).
        qp_base, delta_qp_roi: action ints.
        has_roi_per_frame: list[bool] length nb_frames; if mostly True, bias QP toward ROI.
        out_root: workdir/<name>/x265/

    Returns:
        dict {
            "bitstream_path": str,
            "decoded_yuv": str,
            "decoded_dir": str,
            "total_bytes": int,
            "bitrate_mbps_avg": float,
            "per_frame_bitrate_mbps": [float],
        }
    """
    w = int(meta["width"])
    h = int(meta["height"])
    fps = float(meta["fps"])
    nb = int(meta["nb_frames"])
    src_yuv = meta["yuv_path"]

    roi_ratio = 0.0
    if has_roi_per_frame:
        roi_ratio = float(sum(1 for x in has_roi_per_frame if x)) / max(1, len(has_roi_per_frame))
    eff_qp = qp_base + (delta_qp_roi if roi_ratio >= 0.5 else 0)
    eff_qp = max(0, min(51, eff_qp))

    tag = "qp%d_d%+d" % (qp_base, delta_qp_roi)
    out_dir = os.path.join(out_root, tag)
    decoded_dir = os.path.join(out_dir, "decoded_frames")
    os.makedirs(decoded_dir, exist_ok=True)

    bitstream = os.path.join(out_dir, "bitstream.265")
    decoded_yuv = os.path.join(out_dir, "decoded.yuv")

    cmd_enc = [
        x265_bin,
        "--input", src_yuv,
        "--input-res", "%dx%d" % (w, h),
        "--input-csp", "i420",
        "--fps", str(fps),
        "--frames", str(nb),
        "--qp", str(eff_qp),
        "--preset", "medium",
        "--keyint", "30",
        "--no-info",
        "--log-level", "0",
        "-o", bitstream,
    ]
    subprocess.check_call(cmd_enc, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cmd_dec = [
        ffmpeg_bin, "-y", "-i", bitstream,
        "-pix_fmt", "yuv420p", "-f", "rawvideo",
        decoded_yuv,
    ]
    subprocess.check_call(cmd_dec, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cmd_png = [
        ffmpeg_bin, "-y",
        "-f", "rawvideo", "-pix_fmt", "yuv420p",
        "-s", "%dx%d" % (w, h),
        "-r", str(fps),
        "-i", decoded_yuv,
        "-pix_fmt", "rgb24",
        "-start_number", "0",
        os.path.join(decoded_dir, "%08d.png"),
    ]
    subprocess.check_call(cmd_png, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    total_bytes = os.path.getsize(bitstream)
    duration_sec = nb / fps if fps > 0 else 0.0
    bitrate_mbps_avg = (total_bytes * 8.0 / 1e6) / duration_sec if duration_sec > 0 else 0.0

    per_frame_bytes = _per_frame_bytes(nb, total_bytes)
    per_frame_bitrate_mbps = [b * 8.0 * fps / 1e6 for b in per_frame_bytes]

    info = {
        "bitstream_path": bitstream,
        "decoded_yuv": decoded_yuv,
        "decoded_dir": decoded_dir,
        "total_bytes": total_bytes,
        "bitrate_mbps_avg": bitrate_mbps_avg,
        "per_frame_bitrate_mbps": per_frame_bitrate_mbps,
        "effective_qp": eff_qp,
        "qp_base": qp_base,
        "delta_qp_roi": delta_qp_roi,
    }
    with open(os.path.join(out_dir, "bitrate.json"), "w") as f:
        json.dump(info, f, indent=2)
    return info
