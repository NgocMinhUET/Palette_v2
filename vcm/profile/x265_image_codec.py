# coding=utf-8
"""
Real HEVC (x265) per-image codec for CL-ROI-VCM profile generation.

This replaces the JPEG-quality proxy used in the hf_bdd100k backend with a
genuine HEVC intra-frame encoder (libx265 via ffmpeg). Each image is encoded
as a single HEVC I-frame at the requested QP, yielding realistic
rate-distortion behavior over the QP range used by the action space.

Frame-level ROI handling (approximation):
    delta_qp_roi < 0  (better ROI) → background encoded at qp_base, ROI crops
                                     re-encoded at qp_base + delta_qp_roi and
                                     pasted back over the decoded background.
    delta_qp_roi >= 0                → single-pass encode at qp_base.

This is a CTU-coarser approximation than per-CTU QP delta in HM, but it uses a
real HEVC encoder (libx265) end-to-end, so the bitrate / mAP curves are
qualitatively faithful to what x265 + ROI emphasis would produce.

Returns total bytes (sum of all sub-encodes for ROI mode) and the decoded
PIL.Image. Bitrate at fps=30 is total_bytes * 8 / (1/fps) / 1e6 Mbps.

Requires:
    ffmpeg with libx265 enabled (conda: `ffmpeg` from conda-forge already
    ships with libx265 on Linux). Check via `ffmpeg -encoders | grep libx265`.
"""

from __future__ import print_function

import os
import shutil
import subprocess
import tempfile

from PIL import Image


_FFMPEG_BIN = os.environ.get("VCM_FFMPEG_BIN", "ffmpeg")


def _check_ffmpeg_libx265():
    """Return True if ffmpeg has libx265 encoder available."""
    try:
        out = subprocess.check_output(
            [_FFMPEG_BIN, "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="ignore")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return "libx265" in out


_LIBX265_OK = _check_ffmpeg_libx265()


def libx265_available():
    return _LIBX265_OK


def _ensure_even(value):
    """libx265 requires even dimensions for yuv420p."""
    v = int(value)
    return v - (v % 2)


def _save_padded_png(pil_img, path):
    """Save image with dimensions cropped to even values (libx265 yuv420p)."""
    w, h = pil_img.size
    w2, h2 = _ensure_even(w), _ensure_even(h)
    if (w2, h2) != (w, h):
        pil_img = pil_img.crop((0, 0, w2, h2))
    pil_img.save(path, format="PNG")
    return w2, h2


def _encode_intra_one(in_png, qp, workdir):
    """Encode a single PNG as a one-frame HEVC intra bitstream; return bytes + decoded path."""
    out_265 = os.path.join(workdir, "stream.265")
    out_png = os.path.join(workdir, "decoded.png")
    cmd = [
        _FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-i", in_png,
        "-c:v", "libx265",
        "-x265-params", "qp=%d:keyint=1:tune=zerolatency:log-level=0" % int(qp),
        "-pix_fmt", "yuv420p",
        "-frames:v", "1",
        "-f", "hevc",
        out_265,
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    total_bytes = os.path.getsize(out_265)

    cmd_dec = [
        _FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
        "-i", out_265,
        "-frames:v", "1",
        out_png,
    ]
    subprocess.check_call(cmd_dec, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return total_bytes, out_png


def encode_image_x265(pil_img, qp_base, delta_qp_roi, boxes=None):
    """
    Encode a single PIL image with libx265 at the given QP (ROI-aware).

    Args:
        pil_img: PIL.Image (RGB)
        qp_base: int, base HEVC QP (0..51)
        delta_qp_roi: int, ROI QP offset. Negative → better ROI quality.
        boxes: optional list of [x1, y1, x2, y2]. Only used when delta_qp_roi < 0.

    Returns:
        dict {
            "decoded": PIL.Image,
            "total_bytes": int,         # sum of background + ROI bytes
            "effective_qp_bg": int,
            "effective_qp_roi": int,    # only if ROI mode applied
            "n_roi_patches": int,
        }
    """
    if not _LIBX265_OK:
        raise RuntimeError(
            "ffmpeg libx265 encoder not available. "
            "Install via `conda install -c conda-forge ffmpeg x265 -y` and verify with "
            "`ffmpeg -encoders | grep libx265`."
        )

    qp_bg = max(0, min(51, int(qp_base)))
    qp_roi = max(0, min(51, int(qp_base) + int(delta_qp_roi)))
    pil_rgb = pil_img.convert("RGB")

    tmpdir = tempfile.mkdtemp(prefix="vcm_x265_")
    try:
        in_png = os.path.join(tmpdir, "in.png")
        w, h = _save_padded_png(pil_rgb, in_png)
        pil_rgb_even = Image.open(in_png).convert("RGB")

        bg_dir = os.path.join(tmpdir, "bg")
        os.makedirs(bg_dir, exist_ok=True)
        bg_bytes, bg_decoded_path = _encode_intra_one(in_png, qp_bg, bg_dir)
        bg_decoded = Image.open(bg_decoded_path).convert("RGB")

        n_patches = 0
        total_bytes = bg_bytes
        if delta_qp_roi < 0 and boxes:
            composed = bg_decoded.copy()
            for box in boxes:
                x1, y1, x2, y2 = (
                    max(0, int(box[0])), max(0, int(box[1])),
                    min(w, int(box[2])), min(h, int(box[3])),
                )
                if x2 <= x1 or y2 <= y1:
                    continue
                x1, y1 = _ensure_even(x1), _ensure_even(y1)
                x2, y2 = _ensure_even(x2), _ensure_even(y2)
                if x2 - x1 < 8 or y2 - y1 < 8:
                    continue

                patch_pil = pil_rgb_even.crop((x1, y1, x2, y2))
                patch_dir = os.path.join(tmpdir, "patch_%d_%d" % (x1, y1))
                os.makedirs(patch_dir, exist_ok=True)
                patch_in_png = os.path.join(patch_dir, "p.png")
                patch_pil.save(patch_in_png, format="PNG")
                patch_bytes, patch_decoded_path = _encode_intra_one(
                    patch_in_png, qp_roi, patch_dir
                )
                patch_decoded = Image.open(patch_decoded_path).convert("RGB")
                composed.paste(patch_decoded, (x1, y1))
                total_bytes += patch_bytes
                n_patches += 1
            return {
                "decoded": composed,
                "total_bytes": total_bytes,
                "effective_qp_bg": qp_bg,
                "effective_qp_roi": qp_roi,
                "n_roi_patches": n_patches,
            }

        return {
            "decoded": bg_decoded,
            "total_bytes": total_bytes,
            "effective_qp_bg": qp_bg,
            "effective_qp_roi": qp_bg,
            "n_roi_patches": 0,
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def bitrate_mbps_from_bytes(total_bytes, fps=30.0):
    """Convert per-frame byte count to Mbps at the given fps."""
    if fps <= 0:
        return 0.0
    return (float(total_bytes) * 8.0 / 1e6) * float(fps)
