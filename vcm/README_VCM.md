# CL-ROI-VCM: Cross-Layer ROI-Aware Video Coding for Machine Vision

**English / Tiếng Việt (ngắn):** Extension of the released Palette A3C backbone toward **Video Coding for Machines (VCM)** with a **trace-driven simulator** and **task-utility estimator–based reward**. This is **not** a full WebRTC/Echo reproduction of the original Palette paper.

---

## Motivation

- **Palette** optimizes **human QoE** (e.g., CRF/bitrate vs. delay/rebuffer).
- **CL-ROI-VCM** optimizes **machine task utility** via **QP_base** and **ROI QP offset**, using a lightweight **`u_task_hat`** online (no mAP at inference).

## What changed from Palette

| Aspect | Palette (human RTVC) | CL-ROI-VCM |
|--------|----------------------|------------|
| State | SI/TI, CRF, loss, delay… | Normalized **network + codec + ROI/task** summary (9 dims × history 6) |
| Action | ΔCRF discrete set | **QP_base × ROI_QP_offset** (20 joint actions) |
| Reward | QoE proxy | **`ALPHA_TASK * u_task_hat − bitrate − delay`** (`vcm_config.compute_vcm_reward`) |

## Why not mAP online?

- **mAP** needs labels and heavy inference — suitable **offline** for building profiles and training/evaluating **`TaskUtilityEstimator`**.
- The **controller (train/inference) uses `u_task_hat` consistently** for reward and reporting during RL.
- **`u_task_gt` appears only** in offline CSV profiles, oracle scoring (`compute_oracle_score`), and diagnostics — **never as the online RL reward**.

## Repository layout

- `vcm/` — new modules (`train_vcm.py`, `vcm_env.py`, `a3c_agent_vcm.py`, …).
- `data/offline_profiles/` — CSV profiles (generate dummy via script).
- `data/traces/` — optional mirror; default `--trace_dir traces` uses repo root `traces/` if present.
- `results/vcm_logs/`, `results/vcm_models/` — logs and checkpoints.

Original Palette training files (`train_palette.py`, `a3c_agent.py`, `load_trace.py`) are **unchanged**.

---

## How to run (step by step)

Run from the **repository root** (`Palette_v2/`).

### 1) Dependencies

```bash
pip install -r requirements.txt
# Optional: sklearn-backed utility estimator
pip install -r requirements_vcm.txt
```

### 2) Generate dummy offline profile

```bash
python vcm/build_dummy_offline_profile.py
```

Creates `data/offline_profiles/dummy_vcm_profile.csv` (~120k rows: 10 videos × 600 frames × 20 actions).

### 3) Train A3C controller (recommended: single process)

```bash
python vcm/train_vcm.py --profile_csv data/offline_profiles/dummy_vcm_profile.csv --trace_dir traces --num_agents 1 --episodes 400 --log_dir results/vcm_logs --model_dir results/vcm_models
```

Optional: **`--fit_estimator`** fits `RandomForestRegressor` when **scikit-learn** is installed (otherwise heuristic).

### 4) Evaluate policies

```bash
python vcm/test_vcm.py --policy uniform_qp --profile_csv data/offline_profiles/dummy_vcm_profile.csv --trace_dir traces --log_dir results/vcm_logs --max_steps 4000
python vcm/test_vcm.py --policy fixed_roi --profile_csv data/offline_profiles/dummy_vcm_profile.csv --trace_dir traces --log_dir results/vcm_logs --max_steps 4000
python vcm/test_vcm.py --policy oracle --profile_csv data/offline_profiles/dummy_vcm_profile.csv --trace_dir traces --log_dir results/vcm_logs --max_steps 4000
python vcm/test_vcm.py --policy random --profile_csv data/offline_profiles/dummy_vcm_profile.csv --trace_dir traces --log_dir results/vcm_logs --max_steps 4000
python vcm/test_vcm.py --policy rl --model_path results/vcm_models/nn_model_ep_400.ckpt --profile_csv data/offline_profiles/dummy_vcm_profile.csv --trace_dir traces --log_dir results/vcm_logs --max_steps 4000
```

Results append to **`results/vcm_logs/test_results.csv`**.

### 5) Summary table

```bash
python vcm/evaluate_vcm.py --results_csv results/vcm_logs/test_results.csv
```

Prints `task_per_bitrate = avg_u_task_gt / avg_bitrate` among other aggregates.

---

## Expected outputs

- Training CSV log: `results/vcm_logs/train_single.csv` (columns include QP pair, network observables, `u_task_hat`, `u_task_gt`, reward, action probabilities).
- TensorBoard events under `--log_dir`.
- Checkpoints under `--model_dir` (`nn_model_ep_*.ckpt`).
- Test aggregates in `results/vcm_logs/test_results.csv`.

---

## Next steps with a real dataset

1. Replace **`dummy_vcm_profile.csv`** with rows from real encoded streams + detector/tracker (**`roi_task_extractor.summarize_from_boxes`**).
2. Define **`u_task_gt`** offline (e.g., mAP, IDF1, mIoU — task-dependent).
3. Train **`TaskUtilityEstimator.fit_from_csv`** on that profile; keep **online RL reward tied to `u_task_hat`**.

---

## Scientific constraints (summary)

- Not a full WebRTC reproduction of Palette.
- VCM-oriented extension using the **released Palette RL training backbone**.
- **mAP is not an online reward.**
- **`u_task_hat`**: training + inference controller signal.
- **`u_task_gt`**: offline supervision, oracle/evaluation, logging only.
