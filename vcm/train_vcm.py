# coding=utf-8
"""
Train CL-ROI-VCM controller (A3C backbone from Palette, VCM state/action/reward).

Online reward uses u_task_hat only — not mAP / u_task_gt. This script does not reproduce full WebRTC/Echo.
"""

from __future__ import print_function

import argparse
import csv
import logging
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

config = tf.ConfigProto()
config.gpu_options.allow_growth = True
config.gpu_options.per_process_gpu_memory_fraction = 0.5
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

S_INFO = cfg.S_INFO
S_LEN = cfg.S_LEN
A_DIM = cfg.A_DIM
ACTOR_LR_RATE = 0.00025
CRITIC_LR_RATE = 0.0015
TRAIN_SEQ_LEN = 600
MODEL_SAVE_INTERVAL = 50
RANDOM_SEED = 42
RAND_RANGE = 10000

DEFAULT_ACTION = cfg.A_DIM // 2
NN_MODEL = None


def _resolve_repo_path(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_REPO_ROOT, p))


def central_agent(net_params_queues, exp_queues, agent_processes, episodes_target, model_dir, log_dir):
    assert len(net_params_queues) == len(exp_queues)
    num_agents = len(net_params_queues)

    log_base = os.path.join(log_dir, "train")
    logging.basicConfig(filename=log_base + "_central", filemode="a", level=logging.INFO)

    with tf.Session(config=config) as sess:
        actor = agent.ActorNetwork(sess, state_dim=[S_INFO, S_LEN], action_dim=A_DIM, learning_rate=ACTOR_LR_RATE)
        critic = agent.CriticNetwork(sess, state_dim=[S_INFO, S_LEN], learning_rate=CRITIC_LR_RATE)

        summary_ops, summary_vars = agent.build_summaries()

        sess.run(tf.global_variables_initializer())
        writer = tf.summary.FileWriter(log_dir, sess.graph)
        saver = tf.train.Saver()

        episode = 0
        nn_model = NN_MODEL
        if nn_model is not None:
            saver.restore(sess, nn_model)
            print("Model restored.")

        while True:
            actor_net_params = actor.get_network_params()
            critic_net_params = critic.get_network_params()
            for i in range(num_agents):
                net_params_queues[i].put([actor_net_params, critic_net_params])

            total_reward = 0.0
            total_td_loss = 0.0
            total_entropy = 0.0
            total_agents = 0.0
            min_reward = 0.0

            actor_gradient_batch = []
            critic_gradient_batch = []

            nreward = TRAIN_SEQ_LEN
            for i in range(num_agents):
                s_batch, a_batch, r_batch, terminal, info = exp_queues[i].get()

                actor_gradient, critic_gradient, td_batch = agent.compute_gradients(
                    s_batch=np.stack(s_batch, axis=0),
                    a_batch=np.vstack(a_batch),
                    r_batch=np.vstack(r_batch),
                    terminal=terminal,
                    actor=actor,
                    critic=critic,
                )

                nreward = min(nreward, len(actor_gradient))

                actor_gradient_batch.append(actor_gradient)
                critic_gradient_batch.append(critic_gradient)

                if np.mean(r_batch) < min_reward:
                    min_reward = np.mean(r_batch)
                total_reward += np.mean(r_batch)
                total_td_loss += np.mean(td_batch)
                total_agents += 1.0
                total_entropy += np.mean(info["entropy"])

            actor_gradient_batch = np.divide(actor_gradient_batch, num_agents)
            critic_gradient_batch = np.divide(critic_gradient_batch, num_agents)
            for j in range(1, nreward):
                for i in range(num_agents):
                    actor_gradient_batch[0][j] = np.add(actor_gradient_batch[0][j], actor_gradient_batch[i][j])
                    critic_gradient_batch[0][j] = np.add(critic_gradient_batch[0][j], critic_gradient_batch[i][j])

            mean_actor_gradient_batch = actor_gradient_batch[0]
            mean_critic_gradient_batch = critic_gradient_batch[0]

            actor.apply_gradients(mean_actor_gradient_batch)
            critic.apply_gradients(mean_critic_gradient_batch)

            episode += 1
            avg_reward = total_reward / total_agents
            avg_td_loss = total_td_loss / total_agents
            avg_entropy = total_entropy / total_agents

            logging.info(
                "episode:%06d\tTD_loss:%6.5f\tAvg_reward:%8.2f\tMin_reward:%8.2f\tAvg_entropy:%7.6f"
                % (episode, avg_td_loss, avg_reward, min_reward, avg_entropy)
            )
            print(
                "episode:%06d\tTD_loss:%6.5f\tAvg_reward:%8.2f\tMin_reward:%8.2f\tAvg_entropy:%7.6f"
                % (episode, avg_td_loss, avg_reward, min_reward, avg_entropy)
            )

            summary_str = sess.run(
                summary_ops,
                feed_dict={
                    summary_vars[0]: avg_td_loss,
                    summary_vars[1]: avg_reward,
                    summary_vars[2]: min_reward,
                    summary_vars[3]: avg_entropy,
                },
            )
            writer.add_summary(summary_str, episode)
            writer.flush()

            if episode % MODEL_SAVE_INTERVAL == 0:
                os.makedirs(model_dir, exist_ok=True)
                save_path = saver.save(sess, os.path.join(model_dir, "nn_model_ep_%d.ckpt" % episode))
                logging.info("Model saved in file: " + save_path)

            if episode >= episodes_target:
                for p in agent_processes:
                    if p.is_alive():
                        p.terminate()
                writer.close()
                os._exit(0)


def work_agent(
    agent_id,
    all_cooked_time,
    all_cooked_bw,
    all_file_names,
    net_params_queue,
    exp_queue,
    profile_csv,
    log_dir,
    fit_estimator,
):
    util_est = TaskUtilityEstimator()
    if fit_estimator:
        util_est.fit_from_csv(profile_csv)

    net_env = Environment(
        all_cooked_time=all_cooked_time,
        all_cooked_bw=all_cooked_bw,
        all_file_names=all_file_names,
        random_seed=agent_id + RANDOM_SEED,
        profile_csv=profile_csv,
        utility_estimator=util_est,
    )

    os.makedirs(log_dir, exist_ok=True)
    csv_path = os.path.join(log_dir, "train_agent_%d.csv" % agent_id)
    csv_file = open(csv_path, "w", newline="")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(
        [
            "time",
            "qp_base",
            "delta_qp_roi",
            "bitrate_mbps",
            "bandwidth_mbps",
            "delay_ms",
            "loss",
            "roi_area",
            "obj_count",
            "mean_conf",
            "motion",
            "u_task_hat",
            "u_task_gt",
            "reward",
            "action_prob",
        ]
    )

    config_local = tf.ConfigProto()
    config_local.gpu_options.allow_growth = True
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")

    with tf.Session(config=config_local) as sess:
        actor = agent.ActorNetwork(sess, state_dim=[S_INFO, S_LEN], action_dim=A_DIM, learning_rate=ACTOR_LR_RATE)
        critic = agent.CriticNetwork(sess, state_dim=[S_INFO, S_LEN], learning_rate=CRITIC_LR_RATE)

        sess.run(tf.global_variables_initializer())

        actor_net_params, critic_net_params = net_params_queue.get()
        actor.set_network_params(actor_net_params)
        critic.set_network_params(critic_net_params)

        action = DEFAULT_ACTION
        qp_base, delta_roi = cfg.decode_action(action)

        action_vec = np.zeros(A_DIM)
        action_vec[action] = 1

        s_batch = [np.zeros((S_INFO, S_LEN))]
        a_batch = [action_vec]
        r_batch = []
        entropy_record = []

        time_stamp = 0
        rand_times = 1

        while True:
            obs = net_env.get_video_chunk(qp_base, delta_roi)
            reward = cfg.compute_vcm_reward(obs)

            time_stamp += 1
            r_batch.append(reward)

            vec = get_state_vector(obs)
            state = np.array(s_batch[-1], copy=True)
            state = np.roll(state, -1, axis=1)
            state[:, -1] = vec

            action_prob = actor.predict(np.reshape(state, (1, S_INFO, S_LEN)))

            prob_str = " ".join(str(format(i, ".5f")) for i in action_prob[0])
            csv_writer.writerow(
                [
                    time_stamp,
                    obs["qp_base"],
                    obs["delta_qp_roi"],
                    obs["bitrate_mbps"],
                    obs["bandwidth_mbps"],
                    obs["delay_ms"],
                    obs["loss"],
                    obs["roi_area"],
                    obs["obj_count"],
                    obs["mean_conf"],
                    obs["motion"],
                    obs["u_task_hat"],
                    obs["u_task_gt"],
                    reward,
                    prob_str,
                ]
            )
            csv_file.flush()

            action_cumsum = np.cumsum(action_prob)
            hitcount = np.zeros(A_DIM)
            for _ in range(rand_times):
                hit = (action_cumsum > np.random.randint(1, RAND_RANGE) / float(RAND_RANGE)).argmax()
                hitcount[hit] = hitcount[hit] + 1
            action = int(hitcount.argmax())

            entropy_record.append(agent.compute_entropy(action_prob[0]))

            qp_base, delta_roi = cfg.decode_action(action)

            if len(r_batch) >= TRAIN_SEQ_LEN or obs["end_of_video"]:
                exp_queue.put(
                    [
                        s_batch[1:],
                        a_batch[1:],
                        r_batch[1:],
                        obs["end_of_video"],
                        {"entropy": entropy_record},
                    ]
                )

                actor_net_params, critic_net_params = net_params_queue.get()
                actor.set_network_params(actor_net_params)
                critic.set_network_params(critic_net_params)

                del s_batch[:]
                del a_batch[:]
                del r_batch[:]
                del entropy_record[:]

            if obs["end_of_video"]:
                action = DEFAULT_ACTION
                qp_base, delta_roi = cfg.decode_action(action)
                action_vec = np.zeros(A_DIM)
                action_vec[action] = 1
                s_batch.append(np.zeros((S_INFO, S_LEN)))
                a_batch.append(action_vec)
            else:
                s_batch.append(state)
                action_vec = np.zeros(A_DIM)
                action_vec[action] = 1
                a_batch.append(action_vec)


def train_multi(args, profile_csv, trace_dir, model_dir, log_dir):
    import multiprocess as mp

    net_params_queues = []
    exp_queues = []
    for _ in range(args.num_agents):
        net_params_queues.append(mp.Queue(1))
        exp_queues.append(mp.Queue(1))

    all_cooked_time, all_cooked_bw, all_file_names = load_trace.load_trace(trace_dir)

    agent_processes = []
    for i in range(args.num_agents):
        agent_processes.append(
            mp.Process(
                target=work_agent,
                args=(
                    i,
                    all_cooked_time,
                    all_cooked_bw,
                    all_file_names,
                    net_params_queues[i],
                    exp_queues[i],
                    profile_csv,
                    log_dir,
                    args.fit_estimator,
                ),
            )
        )

    coordinator = mp.Process(
        target=central_agent,
        args=(net_params_queues, exp_queues, agent_processes, args.episodes, model_dir, log_dir),
    )

    coordinator.start()
    for p in agent_processes:
        p.start()

    coordinator.join()


def train_single(args, profile_csv, trace_dir, model_dir, log_dir):
    np.random.seed(args.seed)
    tf.set_random_seed(args.seed)

    util_est = TaskUtilityEstimator()
    if args.fit_estimator:
        util_est.fit_from_csv(profile_csv)

    try:
        all_cooked_time, all_cooked_bw, all_file_names = load_trace.load_trace(trace_dir)
    except Exception:
        all_cooked_time, all_cooked_bw, all_file_names = [], [], []

    net_env = Environment(
        all_cooked_time=all_cooked_time,
        all_cooked_bw=all_cooked_bw,
        all_file_names=all_file_names,
        random_seed=args.seed,
        profile_csv=profile_csv,
        utility_estimator=util_est,
    )

    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(model_dir, exist_ok=True)
    csv_path = os.path.join(log_dir, "train_single.csv")
    csv_file = open(csv_path, "w", newline="")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(
        [
            "time",
            "qp_base",
            "delta_qp_roi",
            "bitrate_mbps",
            "bandwidth_mbps",
            "delay_ms",
            "loss",
            "roi_area",
            "obj_count",
            "mean_conf",
            "motion",
            "u_task_hat",
            "u_task_gt",
            "reward",
            "action_prob",
        ]
    )

    with tf.Session(config=config) as sess:
        actor = agent.ActorNetwork(sess, state_dim=[S_INFO, S_LEN], action_dim=A_DIM, learning_rate=ACTOR_LR_RATE)
        critic = agent.CriticNetwork(sess, state_dim=[S_INFO, S_LEN], learning_rate=CRITIC_LR_RATE)
        summary_ops, summary_vars = agent.build_summaries()
        sess.run(tf.global_variables_initializer())
        writer = tf.summary.FileWriter(log_dir, sess.graph)
        saver = tf.train.Saver()

        action = DEFAULT_ACTION
        qp_base, delta_roi = cfg.decode_action(action)
        action_vec = np.zeros(A_DIM)
        action_vec[action] = 1

        s_batch = [np.zeros((S_INFO, S_LEN))]
        a_batch = [action_vec]
        r_batch = []
        entropy_record = []

        time_stamp = 0
        train_round = 0

        while train_round < args.episodes:
            obs = net_env.get_video_chunk(qp_base, delta_roi)
            reward = cfg.compute_vcm_reward(obs)
            time_stamp += 1
            r_batch.append(reward)

            vec = get_state_vector(obs)
            state = np.array(s_batch[-1], copy=True)
            state = np.roll(state, -1, axis=1)
            state[:, -1] = vec

            action_prob = actor.predict(np.reshape(state, (1, S_INFO, S_LEN)))
            prob_str = " ".join(str(format(i, ".5f")) for i in action_prob[0])
            csv_writer.writerow(
                [
                    time_stamp,
                    obs["qp_base"],
                    obs["delta_qp_roi"],
                    obs["bitrate_mbps"],
                    obs["bandwidth_mbps"],
                    obs["delay_ms"],
                    obs["loss"],
                    obs["roi_area"],
                    obs["obj_count"],
                    obs["mean_conf"],
                    obs["motion"],
                    obs["u_task_hat"],
                    obs["u_task_gt"],
                    reward,
                    prob_str,
                ]
            )

            action_cumsum = np.cumsum(action_prob)
            hitcount = np.zeros(A_DIM)
            for _ in range(1):
                hit = (action_cumsum > np.random.randint(1, RAND_RANGE) / float(RAND_RANGE)).argmax()
                hitcount[hit] += 1
            action = int(hitcount.argmax())
            entropy_record.append(agent.compute_entropy(action_prob[0]))

            qp_base, delta_roi = cfg.decode_action(action)

            if len(r_batch) >= TRAIN_SEQ_LEN or obs["end_of_video"]:
                sb = s_batch[1:]
                ab = a_batch[1:]
                rb = r_batch[1:]
                term = obs["end_of_video"]

                actor_gradient, critic_gradient, td_batch = agent.compute_gradients(
                    s_batch=np.stack(sb, axis=0),
                    a_batch=np.vstack(ab),
                    r_batch=np.vstack(rb),
                    terminal=term,
                    actor=actor,
                    critic=critic,
                )

                actor.apply_gradients(actor_gradient)
                critic.apply_gradients(critic_gradient)

                train_round += 1
                avg_reward = float(np.mean(rb))
                avg_td = float(np.mean(td_batch))
                avg_ent = float(np.mean(entropy_record))

                print(
                    "episode:%06d\tTD_loss:%6.5f\tAvg_reward:%8.2f\tAvg_entropy:%7.6f"
                    % (train_round, avg_td, avg_reward, avg_ent)
                )

                summary_str = sess.run(
                    summary_ops,
                    feed_dict={
                        summary_vars[0]: avg_td,
                        summary_vars[1]: avg_reward,
                        summary_vars[2]: avg_reward,
                        summary_vars[3]: avg_ent,
                    },
                )
                writer.add_summary(summary_str, train_round)
                writer.flush()

                if train_round % MODEL_SAVE_INTERVAL == 0:
                    save_path = saver.save(sess, os.path.join(model_dir, "nn_model_ep_%d.ckpt" % train_round))
                    print("Saved", save_path)

                del s_batch[:]
                del a_batch[:]
                del r_batch[:]
                del entropy_record[:]

            if obs["end_of_video"]:
                action = DEFAULT_ACTION
                qp_base, delta_roi = cfg.decode_action(action)
                action_vec = np.zeros(A_DIM)
                action_vec[action] = 1
                s_batch.append(np.zeros((S_INFO, S_LEN)))
                a_batch.append(action_vec)
            else:
                s_batch.append(state)
                action_vec = np.zeros(A_DIM)
                action_vec[action] = 1
                a_batch.append(action_vec)

        csv_file.flush()
        csv_file.close()
        writer.close()


def parse_args():
    ap = argparse.ArgumentParser(description="Train CL-ROI-VCM (Palette-style A3C)")
    ap.add_argument(
        "--profile_csv",
        type=str,
        default="data/offline_profiles/dummy_vcm_profile.csv",
        help="Offline profile CSV (generate via build_dummy_offline_profile.py)",
    )
    ap.add_argument("--trace_dir", type=str, default="traces", help="Bandwidth traces folder")
    ap.add_argument("--model_dir", type=str, default="results/vcm_models", help="Checkpoint directory")
    ap.add_argument("--log_dir", type=str, default="results/vcm_logs", help="Logs and TensorBoard")
    ap.add_argument("--num_agents", type=int, default=1, help="A3C workers (1 recommended on Windows)")
    ap.add_argument("--episodes", type=int, default=400, help="Coordinator training iterations")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--fit_estimator",
        action="store_true",
        help="Fit TaskUtilityEstimator from profile CSV when sklearn is installed",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    profile_csv = _resolve_repo_path(args.profile_csv)
    trace_dir = _resolve_repo_path(args.trace_dir)
    model_dir = _resolve_repo_path(args.model_dir)
    log_dir = _resolve_repo_path(args.log_dir)

    os.makedirs(model_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    global RANDOM_SEED
    RANDOM_SEED = args.seed

    if args.num_agents <= 1:
        train_single(args, profile_csv, trace_dir, model_dir, log_dir)
    else:
        train_multi(args, profile_csv, trace_dir, model_dir, log_dir)


if __name__ == "__main__":
    main()
