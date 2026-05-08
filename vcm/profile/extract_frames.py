# coding=utf-8
"""
Extract uncompressed frames + raw YUV 4:2:0 from input video clips.

Outputs (per video clip <name>):
    workdir/<name>/frames/00000000.png ... (lossless PNG)
    workdir/<name>/source.yuv          (planar YUV420p, used by HM/x265)
    workdir/<name>/meta.json           (width, height, fps, num_frames)
"""

from __future__ import print_function

import argparse
import glob
import json
import os
import subprocess

from . import paths as P  # noqa: E402


VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi")


def list_videos(folder):
    out = []
    for ext in VIDEO_EXTS:
        out.extend(sorted(glob.glob(os.path.join(folder, "*" + ext))))
    return out


def probe_video(path, ffmpeg_bin):
    cmd = [
        ffmpeg_bin.replace("ffmpeg", "ffprobe"),
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
        "-of", "json",
        path,
    ]
    out = subprocess.check_output(cmd).decode("utf-8")
    info = json.loads(out)["streams"][0]
    w = int(info["width"])
    h = int(info["height"])
    num, den = info["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) > 0 else 30.0
    nb_frames = int(info.get("nb_frames", 0)) if info.get("nb_frames", "0").isdigit() else 0
    return {"width": w, "height": h, "fps": fps, "nb_frames": nb_frames}


def extract_one(video_path, out_dir, ffmpeg_bin, max_seconds=None, target_size=None):
    """
    Extract PNG + YUV from a single clip.
    target_size: optional (W, H) to rescale (keep aspect via -vf scale).
    max_seconds: cap clip duration before extraction (for fast iteration).
    """
    name = os.path.splitext(os.path.basename(video_path))[0]
    clip_dir = os.path.join(out_dir, name)
    frames_dir = os.path.join(clip_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    info = probe_video(video_path, ffmpeg_bin)
    w, h = info["width"], info["height"]
    if target_size is not None:
        w, h = target_size

    duration_args = ["-t", str(max_seconds)] if max_seconds else []
    scale_args = ["-vf", "scale=%d:%d" % (w, h)] if target_size is not None else []

    png_pattern = os.path.join(frames_dir, "%08d.png")
    cmd_png = [
        ffmpeg_bin, "-y", "-i", video_path,
    ] + duration_args + scale_args + [
        "-pix_fmt", "rgb24",
        "-start_number", "0",
        png_pattern,
    ]
    subprocess.check_call(cmd_png, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    yuv_path = os.path.join(clip_dir, "source.yuv")
    cmd_yuv = [
        ffmpeg_bin, "-y", "-i", video_path,
    ] + duration_args + scale_args + [
        "-pix_fmt", "yuv420p",
        "-f", "rawvideo",
        yuv_path,
    ]
    subprocess.check_call(cmd_yuv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    pngs = sorted(glob.glob(os.path.join(frames_dir, "*.png")))
    nb_frames = len(pngs)

    meta = {
        "name": name,
        "video_path": os.path.abspath(video_path),
        "width": w,
        "height": h,
        "fps": info["fps"],
        "nb_frames": nb_frames,
        "yuv_path": os.path.abspath(yuv_path),
        "frames_dir": os.path.abspath(frames_dir),
    }
    with open(os.path.join(clip_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return meta


def parse_args():
    ap = argparse.ArgumentParser(description="Extract YUV+PNG frames from videos for VCM profile.")
    P.add_common_args(ap)
    ap.add_argument("--max_seconds", type=int, default=5, help="Cap duration per clip (sec). 0 = no cap.")
    ap.add_argument("--target_w", type=int, default=1280)
    ap.add_argument("--target_h", type=int, default=720)
    ap.add_argument("--limit_clips", type=int, default=20, help="Number of clips to process (0 = all).")
    return ap.parse_args()


def main():
    args = parse_args()
    if not args.bdd_videos_dir:
        raise SystemExit("--bdd_videos_dir or BDD_VIDEOS_DIR is required.")
    P.ensure_dirs(args.workdir)
    videos = list_videos(args.bdd_videos_dir)
    if args.limit_clips and args.limit_clips > 0:
        videos = videos[: args.limit_clips]
    print("Found %d videos" % len(videos))

    target_size = (args.target_w, args.target_h)
    max_seconds = args.max_seconds if args.max_seconds > 0 else None

    metas = []
    for i, v in enumerate(videos):
        print("[%d/%d] %s" % (i + 1, len(videos), v))
        m = extract_one(v, args.workdir, args.ffmpeg_bin, max_seconds=max_seconds, target_size=target_size)
        metas.append(m)

    index_path = os.path.join(args.workdir, "index.json")
    with open(index_path, "w") as f:
        json.dump(metas, f, indent=2)
    print("Wrote %s (%d clips)" % (index_path, len(metas)))


if __name__ == "__main__":
    main()
