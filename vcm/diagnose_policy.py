# coding=utf-8
"""
Cross-layer policy diagnostic for CL-ROI-VCM (no environment roll-out).

Why this exists
---------------
A 4000-step stochastic test is expensive *and* still gives only a marginal
distribution over actions.  For a VCM controller the academically interesting
question is not "what fraction of steps pick action k" but

    Does pi(a | s) actually change with state, and *which* layer of the state
    (network or vision) drives the change?

This script answers that directly with a few hundred forward passes
(<30 s on CPU).  It produces three deliverables:

  1. SYNTHETIC GRID  -- factorial sweep over (bw, rtt, loss, roi_area, motion,
     obj_count).  Reports per-axis action histograms and the conditional
     entropy decomposition H(a) - H(a | layer), i.e. how much information the
     policy extracts from the network branch vs. the vision branch.

  2. REAL-STATE PROBE -- stratified samples drawn directly from the offline
     profile CSV.  Confirms the synthetic finding on the empirical state
     distribution (no synthetic-vs-real domain gap).

  3. ARGMAX ADAPTIVITY TABLE -- the headline number for the thesis: out of
     all probed states, how many distinct argmax actions does the policy
     select, broken down by state regime?  This is the single quantity
     that separates "true cross-layer adaptation" from "argmax collapse".

The diagnostic loads exactly the same ActorNetwork used in train_vcm.py /
test_vcm.py, so the numbers correspond to the same policy that test_vcm.py
would deploy under --policy rl.
"""

from __future__ import print_function

import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

import numpy as np
import tensorflow as tf

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VCM_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _VCM_ROOT)

import vcm_config as cfg  # noqa: E402
from vcm_env import get_state_vector  # noqa: E402

import a3c_agent_vcm as agent  # noqa: E402

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")


STATE_NAMES = [
    "bw", "rtt", "loss",            # network branch [0:3]
    "qp_prev", "br_prev",           # codec memory   [3:5]
    "roi_area", "obj_count",
    "mean_conf", "motion",          # vision branch  [5:9]
]
NETWORK_IDX = [0, 1, 2]
VISION_IDX = [5, 6, 7, 8]


def _resolve_repo_path(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_REPO_ROOT, p))


def _resolve_checkpoint(model_path, model_dir, model_ep):
    mp = (model_path or "").strip()
    if mp:
        return _resolve_repo_path(mp)
    if not model_dir:
        raise ValueError("Pass --model_path OR --model_dir [--model_ep]")
    d = _resolve_repo_path(model_dir)
    if model_ep is not None:
        cand = os.path.join(d, "nn_model_ep_%d.ckpt" % int(model_ep))
        if not os.path.isfile(cand + ".meta"):
            raise FileNotFoundError(cand + ".meta not found")
        return cand
    metas = glob.glob(os.path.join(d, "nn_model_ep_*.ckpt.meta"))
    best_ep, best_prefix = -1, None
    for m in metas:
        mm = re.match(r"nn_model_ep_(\d+)\.ckpt\.meta$", os.path.basename(m))
        if mm and int(mm.group(1)) > best_ep:
            best_ep = int(mm.group(1))
            best_prefix = m[: -len(".meta")]
    if best_prefix is None:
        raise FileNotFoundError("no nn_model_ep_*.ckpt in " + d)
    return best_prefix


# ---------------------------------------------------------------------------
# State construction helpers
# ---------------------------------------------------------------------------
def make_state_from_obs(obs_dict):
    """Build (S_INFO, S_LEN) state by replicating the same 9-D vec across history.

    Using a constant history is the conservative choice for a diagnostic: any
    state-conditioning we observe comes from the *current* state, not from
    transient temporal patterns that would not be reproducible by the user.
    """
    vec = get_state_vector(obs_dict)
    state = np.tile(vec.reshape(cfg.S_INFO, 1), (1, cfg.S_LEN)).astype(np.float32)
    return state


def synthetic_obs(bw_mbps, rtt_ms, loss, qp_prev, br_prev,
                  roi_area, obj_count, mean_conf, motion):
    return {
        "bandwidth_mbps": bw_mbps,
        "rtt_ms": rtt_ms,
        "loss": loss,
        "qp_base": qp_prev,
        "bitrate_mbps": br_prev,
        "roi_area": roi_area,
        "obj_count": obj_count,
        "mean_conf": mean_conf,
        "motion": motion,
    }


# ---------------------------------------------------------------------------
# Information-theoretic summary
# ---------------------------------------------------------------------------
def entropy_bits(p):
    p = np.asarray(p, dtype=np.float64)
    p = p[p > 0.0]
    return float(-(p * np.log2(p)).sum())


def policy_entropy(probs):
    """Per-state entropy of the softmax."""
    return np.array([entropy_bits(p) for p in probs], dtype=np.float64)


def marginal_action_distribution(probs):
    """Average pi(a | s) across the probed states."""
    return np.mean(np.asarray(probs, dtype=np.float64), axis=0)


def conditional_mutual_info(probs, group_ids):
    """I(A; G) where A ~ argmax pi(.|s) and G is a state-regime label.

    Returns MI in bits.  This is the headline cross-layer-sensitivity number:
      MI ~= 0  -> argmax does not depend on the regime (no adaptation).
      MI > 0  -> argmax differs across regimes (true adaptation).

    We use argmax (not the full softmax) because the deployed test_vcm.py
    uses argmax; this measures *deployed* adaptivity, not training entropy.
    """
    probs = np.asarray(probs)
    group_ids = np.asarray(group_ids)
    actions = np.argmax(probs, axis=1)
    n = len(actions)
    if n == 0:
        return 0.0

    p_a = np.bincount(actions, minlength=cfg.A_DIM) / float(n)
    h_a = entropy_bits(p_a)

    h_a_given_g = 0.0
    for g in np.unique(group_ids):
        mask = group_ids == g
        p_g = mask.mean()
        if p_g == 0.0:
            continue
        sub = actions[mask]
        p_a_g = np.bincount(sub, minlength=cfg.A_DIM) / float(len(sub))
        h_a_given_g += p_g * entropy_bits(p_a_g)
    return float(h_a - h_a_given_g)


# ---------------------------------------------------------------------------
# Probe 1 -- synthetic factorial grid
# ---------------------------------------------------------------------------
def synthetic_grid_probe(actor):
    bw_levels = [2.0, 5.0, 8.0, 12.0]                # Mbps
    rtt_levels = [40.0, 120.0]                       # ms
    loss_levels = [0.0, 0.06]
    roi_levels = [0.05, 0.20, 0.45]                  # fraction
    obj_levels = [2.0, 10.0]
    motion_levels = [5.0, 50.0]

    # Codec memory held at a neutral mid value -- not the variable under test.
    qp_prev_mid = 32
    br_prev_mid = 5.0
    mean_conf_mid = 0.6

    rows = []
    states = []
    for bw in bw_levels:
        for rtt in rtt_levels:
            for loss in loss_levels:
                for roi in roi_levels:
                    for oc in obj_levels:
                        for mo in motion_levels:
                            obs = synthetic_obs(
                                bw, rtt, loss,
                                qp_prev_mid, br_prev_mid,
                                roi, oc, mean_conf_mid, mo,
                            )
                            s = make_state_from_obs(obs)
                            states.append(s)
                            rows.append({
                                "bw": bw, "rtt": rtt, "loss": loss,
                                "roi": roi, "obj_count": oc, "motion": mo,
                            })

    batch = np.stack(states, axis=0)
    probs = actor.predict(batch)
    ents = policy_entropy(probs)
    argmax = np.argmax(probs, axis=1)

    # Regime labels for MI(A; layer-regime).
    bw_label = np.array([0 if r["bw"] < 4 else (1 if r["bw"] < 9 else 2) for r in rows])
    roi_label = np.array([0 if r["roi"] < 0.1 else (1 if r["roi"] < 0.3 else 2) for r in rows])
    motion_label = np.array([0 if r["motion"] < 25 else 1 for r in rows])
    # Joint network regime (bw,rtt,loss) and joint vision regime (roi,obj,motion)
    net_label = np.array([
        (0 if r["bw"] < 4 else (1 if r["bw"] < 9 else 2)) * 4
        + (0 if r["rtt"] < 80 else 1) * 2
        + (0 if r["loss"] < 0.03 else 1)
        for r in rows
    ])
    vis_label = np.array([
        (0 if r["roi"] < 0.1 else (1 if r["roi"] < 0.3 else 2)) * 4
        + (0 if r["obj_count"] < 5 else 1) * 2
        + (0 if r["motion"] < 25 else 1)
        for r in rows
    ])

    summary = {
        "n_states": int(len(rows)),
        "distinct_argmax_actions": int(len(set(argmax.tolist()))),
        "mean_policy_entropy_bits": float(ents.mean()),
        "max_policy_entropy_bits": float(np.log2(cfg.A_DIM)),
        "MI_action_vs_bw_bits": conditional_mutual_info(probs, bw_label),
        "MI_action_vs_roi_bits": conditional_mutual_info(probs, roi_label),
        "MI_action_vs_motion_bits": conditional_mutual_info(probs, motion_label),
        "MI_action_vs_network_regime_bits": conditional_mutual_info(probs, net_label),
        "MI_action_vs_vision_regime_bits": conditional_mutual_info(probs, vis_label),
        "marginal_action_distribution": marginal_action_distribution(probs).tolist(),
        "argmax_counts": {int(k): int(v) for k, v in Counter(argmax.tolist()).items()},
    }

    # Per-axis conditional argmax (most useful for §6.7 of the thesis).
    per_axis = {}
    for name, label in [("bw", bw_label), ("roi", roi_label),
                        ("motion", motion_label),
                        ("network_joint", net_label),
                        ("vision_joint", vis_label)]:
        per_axis[name] = {}
        for g in sorted(set(label.tolist())):
            sub = argmax[label == g]
            cnt = Counter(sub.tolist())
            decoded = {}
            for a_id, n in cnt.most_common():
                qp_b, dq = cfg.decode_action(int(a_id))
                decoded["a=%d (QP=%d, dROI=%+d)" % (a_id, qp_b, dq)] = int(n)
            per_axis[name][int(g)] = decoded
    summary["argmax_by_regime"] = per_axis
    return summary, rows, probs


# ---------------------------------------------------------------------------
# Probe 2 -- real states drawn from the offline profile CSV
# ---------------------------------------------------------------------------
def real_state_probe(actor, profile_csv, n_samples=400, seed=0):
    """Read the offline profile, pick stratified rows, feed through the policy.

    Network branch is taken from the same uniform distribution the env uses
    (we deliberately re-sample (bw,rtt,loss) so that the same vision row is
    probed under several network conditions, exposing cross-layer behavior).
    """
    rng = np.random.RandomState(seed)
    if not os.path.isfile(profile_csv):
        return None, None

    raw_rows = []
    with open(profile_csv, "r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            raw_rows.append(r)
    if not raw_rows:
        return None, None

    # Stratify on roi_area into 4 buckets, sample evenly.
    buckets = defaultdict(list)
    for r in raw_rows:
        try:
            roi = float(r.get("roi_area", 0.0))
        except ValueError:
            continue
        b = min(int(roi / 0.15), 3)
        buckets[b].append(r)
    per_bucket = max(1, n_samples // max(1, len(buckets)))
    picked = []
    for b in sorted(buckets.keys()):
        rows_b = buckets[b]
        if not rows_b:
            continue
        idx = rng.choice(len(rows_b), size=min(per_bucket, len(rows_b)), replace=False)
        picked.extend([rows_b[i] for i in idx])
    if not picked:
        return None, None

    states = []
    labels = []  # joint regime: bw_bin*9 + roi_bin*3 + motion_bin
    for r in picked:
        bw = float(rng.uniform(1.5, cfg.MAX_BANDWIDTH_MBPS))
        rtt = float(rng.uniform(25.0, 180.0))
        loss = float(np.clip(rng.uniform(0.0, 0.08), 0.0, 1.0))

        roi = float(r.get("roi_area", 0.0) or 0.0)
        oc = float(r.get("obj_count", 0.0) or 0.0)
        mc = float(r.get("mean_conf", 0.0) or 0.0)
        mo = float(r.get("motion", 0.0) or 0.0)
        qp = float(r.get("qp_base", 32) or 32)
        br = float(r.get("bitrate_mbps", 5.0) or 5.0)

        obs = synthetic_obs(bw, rtt, loss, qp, br, roi, oc, mc, mo)
        states.append(make_state_from_obs(obs))

        bw_bin = 0 if bw < 4 else (1 if bw < 9 else 2)
        roi_bin = 0 if roi < 0.1 else (1 if roi < 0.3 else 2)
        mo_bin = 0 if mo < 25 else 1
        labels.append(bw_bin * 9 + roi_bin * 3 + mo_bin)

    batch = np.stack(states, axis=0)
    probs = actor.predict(batch)
    argmax = np.argmax(probs, axis=1)
    ents = policy_entropy(probs)

    summary = {
        "n_real_states": int(len(states)),
        "distinct_argmax_actions": int(len(set(argmax.tolist()))),
        "mean_policy_entropy_bits": float(ents.mean()),
        "MI_action_vs_joint_regime_bits": conditional_mutual_info(
            probs, np.asarray(labels)
        ),
        "argmax_counts": {int(k): int(v) for k, v in Counter(argmax.tolist()).items()},
        "marginal_action_distribution": marginal_action_distribution(probs).tolist(),
    }

    # Drill-down: per (bw_bin, roi_bin) -> top action.
    joint = defaultdict(list)
    for lbl, a in zip(labels, argmax):
        bw_bin = lbl // 9
        roi_bin = (lbl % 9) // 3
        joint[(bw_bin, roi_bin)].append(int(a))
    drill = {}
    for (b, r), acts in sorted(joint.items()):
        cnt = Counter(acts)
        top_a, top_n = cnt.most_common(1)[0]
        qp_b, dq = cfg.decode_action(int(top_a))
        drill["bw_bin=%d_roi_bin=%d" % (b, r)] = {
            "n": len(acts),
            "top_action": int(top_a),
            "top_action_qp_droi": [int(qp_b), int(dq)],
            "top_action_share": float(top_n) / float(len(acts)),
        }
    summary["argmax_by_bw_roi"] = drill
    return summary, None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def render_verdict(syn, real):
    lines = []
    lines.append("=" * 72)
    lines.append("CROSS-LAYER POLICY DIAGNOSTIC -- VERDICT")
    lines.append("=" * 72)

    def fmt_mi(name, v):
        max_h = float(np.log2(cfg.A_DIM))
        return "  %-38s = %6.3f bits  (max ~ log2(%d) = %.2f)" % (
            name, v, cfg.A_DIM, max_h
        )

    lines.append("[Synthetic grid -- %d states]" % syn["n_states"])
    lines.append("  distinct argmax actions      = %d / %d" %
                 (syn["distinct_argmax_actions"], cfg.A_DIM))
    lines.append("  mean policy entropy          = %.3f bits  (max %.2f)" %
                 (syn["mean_policy_entropy_bits"], syn["max_policy_entropy_bits"]))
    lines.append(fmt_mi("MI(argmax ; bw)",            syn["MI_action_vs_bw_bits"]))
    lines.append(fmt_mi("MI(argmax ; roi)",           syn["MI_action_vs_roi_bits"]))
    lines.append(fmt_mi("MI(argmax ; motion)",        syn["MI_action_vs_motion_bits"]))
    lines.append(fmt_mi("MI(argmax ; network_joint)", syn["MI_action_vs_network_regime_bits"]))
    lines.append(fmt_mi("MI(argmax ; vision_joint)",  syn["MI_action_vs_vision_regime_bits"]))

    if real is not None:
        lines.append("")
        lines.append("[Real-profile probe -- %d states]" % real["n_real_states"])
        lines.append("  distinct argmax actions      = %d / %d" %
                     (real["distinct_argmax_actions"], cfg.A_DIM))
        lines.append("  mean policy entropy          = %.3f bits" %
                     real["mean_policy_entropy_bits"])
        lines.append(fmt_mi("MI(argmax ; joint_regime)",
                            real["MI_action_vs_joint_regime_bits"]))

    # One-line verdict for the thesis.
    net_mi = syn["MI_action_vs_network_regime_bits"]
    vis_mi = syn["MI_action_vs_vision_regime_bits"]
    distinct = syn["distinct_argmax_actions"]
    lines.append("")
    lines.append("-- Verdict --")
    if distinct <= 1 and net_mi < 0.05 and vis_mi < 0.05:
        lines.append("  COLLAPSED:    policy ignores both layers; argmax is constant.")
    elif distinct >= 2 and net_mi >= 0.05 and vis_mi >= 0.05:
        lines.append("  CROSS-LAYER:  argmax depends on BOTH network and vision regimes.")
    elif distinct >= 2 and net_mi >= 0.05:
        lines.append("  NETWORK-ONLY: argmax reacts to network state but not vision state.")
    elif distinct >= 2 and vis_mi >= 0.05:
        lines.append("  VISION-ONLY:  argmax reacts to vision state but not network state.")
    else:
        lines.append("  WEAK:         softmax has spread but argmax adaptation is marginal.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model_path", default="")
    ap.add_argument("--model_dir", default="")
    ap.add_argument("--model_ep", type=int, default=None)
    ap.add_argument("--profile_csv",
                    default="data/offline_profiles/bdd100k_x265_intra_profile.csv")
    ap.add_argument("--n_real_samples", type=int, default=400)
    ap.add_argument("--out_json", default="")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    ckpt = _resolve_checkpoint(args.model_path, args.model_dir, args.model_ep)
    print("[diagnose] checkpoint:", ckpt)

    sess = tf.Session()
    actor = agent.ActorNetwork(
        sess,
        state_dim=[cfg.S_INFO, cfg.S_LEN],
        action_dim=cfg.A_DIM,
        learning_rate=0.00025,
    )
    sess.run(tf.global_variables_initializer())
    tf.train.Saver().restore(sess, ckpt)

    syn_summary, _, _ = synthetic_grid_probe(actor)
    profile_path = _resolve_repo_path(args.profile_csv)
    real_summary, _ = real_state_probe(
        actor, profile_path, n_samples=args.n_real_samples, seed=args.seed
    )

    verdict = render_verdict(syn_summary, real_summary)
    print(verdict)

    out = {
        "checkpoint": ckpt,
        "profile_csv": profile_path,
        "synthetic": syn_summary,
        "real": real_summary,
        "verdict_text": verdict,
    }
    if args.out_json:
        out_path = _resolve_repo_path(args.out_json)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2)
        print("[diagnose] wrote", out_path)

    sess.close()


if __name__ == "__main__":
    main()
