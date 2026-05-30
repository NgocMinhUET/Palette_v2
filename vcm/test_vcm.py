# coding=utf-8
"""
Evaluate CL-ROI-VCM policies (RL checkpoint, baselines, oracle upper bound from offline u_task_gt).

Oracle uses u_task_gt only for offline upper-bound comparison — not as an online controller signal.
"""

from __future__ import print_function

import argparse
import csv
import glob
import json
import os
import re
import sys

import numpy as np
import tensorflow as tf

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VCM_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _VCM_ROOT)

import load_trace  # noqa: E402
import vcm_config as cfg  # noqa: E402
from task_utility_estimator import TaskUtilityEstimator  # noqa: E402
from vcm_env import Environment, get_state_vector  # noqa: E402
from policy_baselines import (  # noqa: E402
    cl_roi_rule_action,
    palette_xlayer_action,
    resolve_ctc_anchor_qp,
    vcm_pure_action,
)

import a3c_agent_vcm as agent  # noqa: E402

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")


def _resolve_repo_path(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_REPO_ROOT, p))


def _resolve_trace_dir(p):
    """Ensure trailing separator — load_trace.py uses string concatenation, not os.path.join."""
    resolved = _resolve_repo_path(p)
    if not resolved.endswith(os.sep):
        resolved = resolved + os.sep
    return resolved


def _resolve_rl_checkpoint_prefix(model_path, model_dir, model_ep):
    """Return TF saver checkpoint prefix (path ending in .ckpt, no .meta)."""
    mp = (model_path or "").strip()
    if mp:
        return _resolve_repo_path(mp)
    md = (model_dir or "").strip()
    if not md:
        return ""
    d = _resolve_repo_path(md)
    if model_ep is not None:
        cand = os.path.join(d, "nn_model_ep_%d.ckpt" % int(model_ep))
        if not os.path.isfile(cand + ".meta"):
            raise FileNotFoundError(
                "No checkpoint at %s (.meta missing); check --model_ep / --model_dir" % cand
            )
        return cand
    metas = glob.glob(os.path.join(d, "nn_model_ep_*.ckpt.meta"))
    if not metas:
        raise FileNotFoundError(
            "No nn_model_ep_*.ckpt under %s; pass --model_path explicitly" % d
        )
    best_prefix = None
    best_ep = -1
    for meta in metas:
        base = os.path.basename(meta)
        m = re.match(r"nn_model_ep_(\d+)\.ckpt\.meta$", base)
        if not m:
            continue
        ep = int(m.group(1))
        if ep > best_ep:
            best_ep = ep
            best_prefix = meta[: -len(".meta")]
    if best_prefix is None:
        raise FileNotFoundError("Could not parse nn_model_ep_* checkpoints in %s" % d)
    return best_prefix


def _fixed_action_id(qp_base, delta_roi):
    return cfg.encode_action(qp_base, delta_roi)


def run_policy(args, profile_csv, trace_dir):
    try:
        all_cooked_time, all_cooked_bw, all_file_names = load_trace.load_trace(trace_dir)
    except Exception:
        all_cooked_time, all_cooked_bw, all_file_names = [], [], []

    util_est = TaskUtilityEstimator()
    env = Environment(
        all_cooked_time=all_cooked_time,
        all_cooked_bw=all_cooked_bw,
        all_file_names=all_file_names,
        random_seed=args.seed,
        profile_csv=profile_csv,
        utility_estimator=util_est,
    )

    policy = args.policy
    action_counts = np.zeros(cfg.A_DIM, dtype=np.int64)

    rewards = []
    u_hat = []
    u_gt = []
    bitrates = []
    delays = []
    losses = []

    sess = None
    actor = None
    if policy == "rl":
        sess = tf.Session()
        actor = agent.ActorNetwork(
            sess,
            state_dim=[cfg.S_INFO, cfg.S_LEN],
            action_dim=cfg.A_DIM,
            learning_rate=0.00025,
        )
        sess.run(tf.global_variables_initializer())
        saver = tf.train.Saver()
        saver.restore(sess, args.model_path)

    state = np.zeros((cfg.S_INFO, cfg.S_LEN))
    last_qp = int(cfg.QP_BASE_SET[len(cfg.QP_BASE_SET) // 2])

    ctc_anchor_qp = None
    if policy in ("vcm_pure", "vcm_ctc_anchor"):
        ctc_anchor_qp = resolve_ctc_anchor_qp(
            profile_csv,
            all_cooked_bw=all_cooked_bw,
        )
        print(
            "CTC anchor (MPEG VCM conventional video): qp_base=%d dROI=0 "
            "(capacity-matched on ladder; RD curve: evaluate_bd_acc "
            "ladder_uniform / vcm_ctc_anchor)"
            % ctc_anchor_qp
        )

    steps = 0
    while steps < args.max_steps:
        if policy in ("vcm_pure", "vcm_ctc_anchor"):
            action_id, qp_base, delta_roi = vcm_pure_action(ctc_anchor_qp)
            obs = env.get_video_chunk(qp_base, delta_roi)
        elif policy == "palette_xlayer":
            feat = env.current_decision_features()
            action_id, qp_base, delta_roi = palette_xlayer_action(feat, last_qp)
            obs = env.get_video_chunk(qp_base, delta_roi)
            last_qp = int(qp_base)
        elif policy == "cl_roi_rule":
            feat = env.current_decision_features()
            action_id, qp_base, delta_roi = cl_roi_rule_action(feat, last_qp)
            obs = env.get_video_chunk(qp_base, delta_roi)
            last_qp = int(qp_base)
        elif policy == "oracle":
            cand = env.peek_action_observations()
            scores = []
            for o in cand:
                scores.append(cfg.compute_oracle_score(o))
            best_i = int(np.argmax(np.array(scores)))
            qp_base, delta_roi = cfg.decode_action(best_i)
            obs = env.get_video_chunk(qp_base, delta_roi)
            action_id = best_i
        elif policy == "uniform_qp":
            action_id = _fixed_action_id(32, 0)
            qp_base, delta_roi = cfg.decode_action(action_id)
            obs = env.get_video_chunk(qp_base, delta_roi)
        elif policy == "fixed_roi":
            action_id = _fixed_action_id(32, -3)
            qp_base, delta_roi = cfg.decode_action(action_id)
            obs = env.get_video_chunk(qp_base, delta_roi)
        elif policy == "random":
            action_id = int(np.random.randint(0, cfg.A_DIM))
            qp_base, delta_roi = cfg.decode_action(action_id)
            obs = env.get_video_chunk(qp_base, delta_roi)
        elif policy == "rl":
            # Causal alignment: decide using CURRENT frame content + network.
            feat = env.current_decision_features()
            vec = get_state_vector(feat)
            state = np.roll(state, -1, axis=1)
            state[:, -1] = vec
            prob = actor.predict(np.reshape(state, (1, cfg.S_INFO, cfg.S_LEN)))[0]
            if getattr(args, "stochastic", False):
                prob = np.asarray(prob, dtype=np.float64)
                prob = prob / max(prob.sum(), 1e-9)
                action_id = int(np.random.choice(cfg.A_DIM, p=prob))
            else:
                action_id = int(np.argmax(prob))
            qp_base, delta_roi = cfg.decode_action(action_id)
            obs = env.get_video_chunk(qp_base, delta_roi)
        else:
            raise ValueError("Unknown policy %s" % policy)

        reward = cfg.compute_vcm_reward(obs)
        rewards.append(reward)
        u_hat.append(obs["u_task_hat"])
        u_gt.append(obs["u_task_gt"])
        bitrates.append(obs["bitrate_mbps"])
        delays.append(obs["delay_ms"])
        losses.append(obs["loss"])
        action_counts[action_id] += 1

        steps += 1

        if obs["end_of_video"]:
            env.reset_episode()
            last_qp = int(cfg.QP_BASE_SET[len(cfg.QP_BASE_SET) // 2])
            if policy == "rl":
                state = np.zeros((cfg.S_INFO, cfg.S_LEN))

    if sess is not None:
        sess.close()

    dist = {str(i): int(action_counts[i]) for i in range(cfg.A_DIM)}
    row = {
        "policy": policy,
        "steps": steps,
        "avg_reward": float(np.mean(rewards)) if rewards else 0.0,
        "avg_u_task_hat": float(np.mean(u_hat)) if u_hat else 0.0,
        "avg_u_task_gt": float(np.mean(u_gt)) if u_gt else 0.0,
        "avg_bitrate": float(np.mean(bitrates)) if bitrates else 0.0,
        "avg_delay": float(np.mean(delays)) if delays else 0.0,
        "avg_loss": float(np.mean(losses)) if losses else 0.0,
        "action_distribution_json": json.dumps(dist),
    }
    return row


def append_results_csv(log_dir, row):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "test_results.csv")
    write_header = not os.path.isfile(path)
    fields = [
        "policy",
        "steps",
        "avg_reward",
        "avg_u_task_hat",
        "avg_u_task_gt",
        "avg_bitrate",
        "avg_delay",
        "avg_loss",
        "action_distribution_json",
    ]
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        w.writerow(row)


def parse_args():
    ap = argparse.ArgumentParser(description="Test CL-ROI-VCM policies")
    ap.add_argument(
        "--model_path",
        type=str,
        default="",
        help="TensorFlow checkpoint prefix for rl (e.g. results/vcm_models/nn_model_ep_400.ckpt)",
    )
    ap.add_argument(
        "--model_dir",
        type=str,
        default="",
        help="Directory containing nn_model_ep_*.ckpt; uses latest episode if --model_path omitted",
    )
    ap.add_argument(
        "--model_ep",
        type=int,
        default=None,
        help="With --model_dir, load nn_model_ep_{ep}.ckpt instead of latest",
    )
    ap.add_argument(
        "--profile_csv",
        type=str,
        default="data/offline_profiles/dummy_vcm_profile.csv",
    )
    ap.add_argument("--trace_dir", type=str, default="traces")
    ap.add_argument("--log_dir", type=str, default="results/vcm_logs")
    ap.add_argument(
        "--policy",
        type=str,
        default="all",
        help="One of: rl, oracle, uniform_qp, fixed_roi, random, vcm_pure, "
             "vcm_ctc_anchor, palette_xlayer, cl_roi_rule, all (default: all)",
    )
    ap.add_argument("--max_steps", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--stochastic",
        action="store_true",
        help="For --policy rl: sample action from policy distribution instead of argmax. "
             "Useful to check whether the trained policy is state-adaptive.",
    )
    return ap.parse_args()


_ALL_POLICIES = [
    "vcm_pure",
    "palette_xlayer",
    "uniform_qp",
    "fixed_roi",
    "cl_roi_rule",
    "random",
    "oracle",
    "rl",
]


def main():
    args = parse_args()
    profile_csv = _resolve_repo_path(args.profile_csv)
    trace_dir = _resolve_trace_dir(args.trace_dir)
    log_dir = _resolve_repo_path(args.log_dir)
    np.random.seed(args.seed)

    policies_to_run = _ALL_POLICIES if args.policy == "all" else [args.policy]

    # Pre-resolve RL checkpoint once if needed
    rl_checkpoint = ""
    if "rl" in policies_to_run:
        try:
            rl_checkpoint = _resolve_rl_checkpoint_prefix(
                args.model_path, args.model_dir, args.model_ep
            )
        except FileNotFoundError as e:
            print("[WARN] Skipping rl policy: %s" % str(e))
            policies_to_run = [p for p in policies_to_run if p != "rl"]
        if not rl_checkpoint and "rl" in policies_to_run:
            print("[WARN] Skipping rl policy: pass --model_path or --model_dir")
            policies_to_run = [p for p in policies_to_run if p != "rl"]

    # Remove stale CSV so we start fresh when running all policies
    if args.policy == "all":
        stale = os.path.join(log_dir, "test_results.csv")
        if os.path.isfile(stale):
            os.remove(stale)
            print("Removed stale %s" % stale)

    for policy in policies_to_run:
        args.policy = policy
        if policy == "rl":
            args.model_path = rl_checkpoint
        print("Running policy=%s ..." % policy)
        row = run_policy(args, profile_csv, trace_dir)
        append_results_csv(log_dir, row)
        print(row)


if __name__ == "__main__":
    main()
