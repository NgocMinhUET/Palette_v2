# Real Offline Profile Pipeline (Phase 2)

Builds a real CSV profile (`real_bdd_profile.csv`) compatible with `vcm/vcm_env.py`
and `vcm/train_vcm.py`. Replaces the synthetic `dummy_vcm_profile.csv`.

## Methodology

| Step | Tool | Output |
|------|------|--------|
| Extract uncompressed frames | ffmpeg | `workdir/<name>/{frames/, source.yuv}` |
| YOLOv8 on uncompressed | ultralytics | per-frame bbox = **pseudo-GT** for mAP |
| Encode-decode (x265 fast / HM rigorous) | libx265 / HM | `bitstream + decoded_frames` |
| YOLOv8 on decoded | ultralytics | per-frame bbox (predicted) |
| mAP@[0.5:0.95] (pred vs pseudo-GT) | numpy | `u_task_gt ∈ [0,1]` per frame |
| Sample (bw, rtt, loss) | numpy | per-frame network conditions |

This `pred-vs-uncompressed` formulation is the standard practice in the JVET
CTC for Machines and CompressAI-Vision; it avoids dataset-specific GT format
wrangling and isolates the **codec-induced task degradation** that the VCM
controller is supposed to react to.

## Required prerequisites (Phase 2A)

```bash
pip install ultralytics torchmetrics pycocotools
sudo apt install ffmpeg x265   # or conda install -c conda-forge ffmpeg x265
```

For the rigorous (paper) HM path (currently stubbed):

```bash
git clone https://vcgit.hhi.fraunhofer.de/jvet/HM
cd HM/build/linux && make -j8
```

## Quick run (x265 fast iteration)

From repo root:

```bash
export BDD_VIDEOS_DIR=/path/to/bdd100k_subset
export YOLO_WEIGHTS=yolov8m.pt   # auto-downloaded by ultralytics on first run

python -m vcm.profile.build_real_profile \
  --encoder x265 \
  --limit_clips 20 \
  --max_seconds 5 \
  --target_w 1280 --target_h 720 \
  --workdir data/profile_work \
  --out_csv data/offline_profiles/real_bdd_profile.csv
```

Estimated runtime: ~3-5 min/clip on a single GPU (RTX 3060+) with x265 medium.
20 clips ≈ 60-100 min for a full 5×4 = 20 action grid.

## Then re-train and evaluate with the real profile

```bash
python vcm/train_vcm.py \
  --profile_csv data/offline_profiles/real_bdd_profile.csv \
  --num_agents 1 --episodes 800 \
  --fit_estimator \
  --log_dir results/vcm_logs --model_dir results/vcm_models

python vcm/test_vcm.py --policy oracle     --profile_csv data/offline_profiles/real_bdd_profile.csv
python vcm/test_vcm.py --policy uniform_qp --profile_csv data/offline_profiles/real_bdd_profile.csv
python vcm/test_vcm.py --policy fixed_roi  --profile_csv data/offline_profiles/real_bdd_profile.csv
python vcm/test_vcm.py --policy random     --profile_csv data/offline_profiles/real_bdd_profile.csv
python vcm/test_vcm.py --policy rl --model_path results/vcm_models/nn_model_ep_800.ckpt \
                      --profile_csv data/offline_profiles/real_bdd_profile.csv

python vcm/evaluate_vcm.py
```

## Smoke test (very small, ~10 min)

```bash
python -m vcm.profile.build_real_profile \
  --bdd_videos_dir /path/to/2_or_3_clips/ \
  --limit_clips 2 --max_seconds 2 \
  --out_csv data/offline_profiles/real_bdd_smoke.csv
```

Expect ~4,800 rows (2 clips × 60 frames × 20 actions). Use this to verify all
binaries (ffmpeg, x265, ultralytics) are reachable before launching the
full subset.

## HM (paper-grade) integration plan

`encode_hm.py` currently raises `NotImplementedError`. It will be enabled once
the x265 numbers look reasonable:

1. Generate per-frame, per-CTU QP-delta map from pseudo-GT bboxes
   (`_build_qp_delta_map` is already implemented).
2. Invoke `TAppEncoderStatic` with `--MaxCUWidth=64 --MaxCUHeight=64
   --MaxCUDepth=4 --QP=<qp_base> --MaxDeltaQP=<|delta_qp_roi|>` plus the
   custom `--dQPFile` patch (or use HM-VCM CTC fork that natively accepts
   QP delta files).
3. Decode with `TAppDecoderStatic`.
4. Pipe YUV → PNG with ffmpeg (same as x265 path).

## Notes

- ROI in x265 is approximated at frame level (qp_base + delta when ROI present).
  HM is needed for true per-CTU ROI QP control. See `encode_x265.py` docstring.
- If you have BDD ground truth labels and want to compute mAP against them
  instead of YOLO-on-uncompressed, replace `gt_dets` in `_process_clip` with
  a parser of your `det_20/*.json` files.
