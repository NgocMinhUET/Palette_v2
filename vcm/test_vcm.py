# coding=utf-8
"""
Evaluate CL-ROI-VCM policies (RL checkpoint, baselines, oracle upper bound from offline u_task_gt).

Oracle uses u_task_gt only for offline upper-bound comparison — not as an online controller signal.
"""

from __future__ import print_function

import argparse
import csv
import json
import os
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

import a3c_agent_vcm as agent  # noqa: E402

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")


def _resolve_repo_path(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_REPO_ROOT, p))


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

    steps = 0
    while steps < args.max_steps:
        if policy == "oracle":
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
            prob = actor.predict(np.reshape(state, (1, cfg.S_INFO, cfg.S_LEN)))[0]
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

        if policy == "rl":
            vec = get_state_vector(obs)
            state = np.roll(state, -1, axis=1)
            state[:, -1] = vec

        if obs["end_of_video"]:
            env.reset_episode()

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
    ap.add_argument("--model_path", type=str, default="", help="TensorFlow checkpoint prefix for rl policy")
    ap.add_argument(
        "--profile_csv",
        type=str,
        default="data/offline_profiles/dummy_vcm_profile.csv",
    )
    ap.add_argument("--trace_dir", type=str, default="traces")
    ap.add_argument("--log_dir", type=str, default="results/vcm_logs")
    ap.add_argument("--policy", type=str, default="uniform_qp")
    ap.add_argument("--max_steps", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=0)
    return ap.parse_args()


def main():
    args = parse_args()
    if args.policy == "rl" and not args.model_path:
        print("--model_path is required when --policy rl")
        sys.exit(1)
    profile_csv = _resolve_repo_path(args.profile_csv)
    trace_dir = _resolve_repo_path(args.trace_dir)
    log_dir = _resolve_repo_path(args.log_dir)

    np.random.seed(args.seed)

    print("Running policy=%s ..." % args.policy)
    row = run_policy(args, profile_csv, trace_dir)
    append_results_csv(log_dir, row)
    print(row)


if __name__ == "__main__":
    main()
