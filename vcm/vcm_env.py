# coding=utf-8
"""
Trace-driven VCM simulator: offline profile lookup + optional bandwidth traces.

This is not a full WebRTC reproduction of Palette. It reuses the A3C training loop idea with
VCM-oriented observations; online reward uses u_task_hat only (mAP / u_task_gt stay offline).
"""

import csv
import os

import numpy as np

import vcm_config as cfg
from task_utility_estimator import TaskUtilityEstimator


class Environment(object):
    """
    Steps through synthetic/offline CSV profiles and optionally overlays trace bandwidth.

    Network conditions (bandwidth, RTT, loss) are properties of the current
    *timestep*, not of the action chosen at that step.  A single set of network
    conditions is sampled at the start of each step and held constant across all
    possible actions so that the reward difference between actions reflects only
    the codec RD trade-off — not spurious bandwidth noise baked into the profile.
    """

    # Default; overridden per video_id after the profile is loaded.
    FRAMES_PER_VIDEO = 600

    def __init__(
        self,
        all_cooked_time,
        all_cooked_bw,
        all_file_names,
        random_seed=0,
        profile_csv=None,
        utility_estimator=None,
    ):
        self.all_cooked_time = all_cooked_time or []
        self.all_cooked_bw = all_cooked_bw or []
        self.all_file_names = all_file_names or []
        self.random_seed = random_seed
        self.rng = np.random.RandomState(random_seed)

        if profile_csv is None:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            profile_csv = os.path.join(root, "data", "offline_profiles", "dummy_vcm_profile.csv")
        self.profile_csv = os.path.abspath(profile_csv)

        self._lookup = {}
        self._video_ids = []
        self._load_profile()

        self.trace_pkt_idx = 0
        if len(self.all_cooked_bw) > 0:
            self._episode_trace_snippet = int(self.rng.randint(0, len(self.all_cooked_bw)))
        else:
            self._episode_trace_snippet = 0

        self.util_est = utility_estimator if utility_estimator is not None else TaskUtilityEstimator()

        self.video_ix = 0
        self.frame_ix = 0

        # Pre-sample network conditions for the FIRST step.  These are refreshed
        # after every call to get_video_chunk() so that all 20 possible actions
        # at the same step see identical network conditions.
        self._step_bw, self._step_rtt, self._step_loss = self._draw_step_network()

        # Track the most recently APPLIED action so the pre-decision state can
        # carry "previous action" history (qp_prev / bitrate_prev) without
        # leaking the current action's outcome.  Initialised to neutral values.
        self._last_qp = int(np.median(cfg.QP_BASE_SET))
        self._last_bitrate = float(cfg.MAX_BITRATE_MBPS) * 0.3

    def _load_profile(self):
        self._lookup.clear()
        self._video_ids = []
        # Track max frame_id per video to set episode length dynamically.
        _max_fid = {}
        with open(self.profile_csv, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                vid = int(row["video_id"])
                fid = int(row["frame_id"])
                qb = int(row["qp_base"])
                dr = int(row["delta_qp_roi"])
                key = (vid, fid, qb, dr)
                self._lookup[key] = row
                if vid not in self._video_ids:
                    self._video_ids.append(vid)
                if vid not in _max_fid or fid > _max_fid[vid]:
                    _max_fid[vid] = fid
        self._video_ids = sorted(self._video_ids)
        if not self._video_ids:
            raise ValueError("Empty profile CSV: %s" % self.profile_csv)
        # frames_per_video[vid] = number of valid frame indices (0 .. max_fid inclusive)
        self._frames_per_video = {vid: _max_fid[vid] + 1 for vid in self._video_ids}

    def _draw_step_network(self):
        """Return (bw_mbps, rtt_ms, loss) for the current step.

        Bandwidth is taken from the bandwidth trace when available; otherwise
        sampled uniformly.  RTT and loss are always freshly sampled so that
        temporal variation is independent of which action the agent picks.
        """
        bw_trace = self._trace_bw_at_cursor()
        if bw_trace is not None:
            bw = float(bw_trace)
        else:
            bw = float(self.rng.uniform(1.5, cfg.MAX_BANDWIDTH_MBPS))
        rtt = float(self.rng.uniform(25.0, 180.0))
        loss = float(np.clip(self.rng.uniform(0.0, 0.08), 0.0, 1.0))
        return bw, rtt, loss

    def _current_vid_fid(self):
        vid = self._video_ids[self.video_ix % len(self._video_ids)]
        fid = self.frame_ix
        return vid, fid

    def _trace_bw_at_cursor(self):
        """Current trace bandwidth without consuming the cursor (peek-friendly)."""
        if not self.all_cooked_bw:
            return None
        snippet = self.all_cooked_bw[self._episode_trace_snippet % len(self.all_cooked_bw)]
        if not snippet:
            return None
        idx = self.trace_pkt_idx % len(snippet)
        return float(snippet[idx])

    def _advance_trace_cursor(self):
        self.trace_pkt_idx += 1

    def _row_to_obs(self, row, bandwidth_mbps_effective, rtt_ms_override=None, loss_override=None):
        # Network conditions come from the step-level sampled values, not from
        # the per-action profile row (which has arbitrary random values).
        rtt_ms = rtt_ms_override if rtt_ms_override is not None else float(row["rtt_ms"])
        loss_csv = loss_override if loss_override is not None else float(row["loss"])
        bitrate_mbps = float(row["bitrate_mbps"])
        qp_base = int(row["qp_base"])
        delta_qp_roi = int(row["delta_qp_roi"])
        roi_area = float(row["roi_area"])
        obj_count = int(row["obj_count"])
        mean_conf = float(row["mean_conf"])
        motion = float(row["motion"])
        u_task_gt = float(row["u_task_gt"])

        bw = float(bandwidth_mbps_effective)
        delay_ms = rtt_ms + max(0.0, bitrate_mbps - bw) * 100.0
        overshoot = max(0.0, bitrate_mbps - bw) / max(cfg.MAX_BANDWIDTH_MBPS, 1e-6)
        loss_eff = float(np.clip(loss_csv + 0.08 * overshoot, 0.0, 1.0))

        feat = {
            "roi_area": roi_area,
            "obj_count": obj_count,
            "mean_conf": mean_conf,
            "motion": motion,
            "qp_base": qp_base,
            "delta_qp_roi": delta_qp_roi,
            "bitrate_mbps": bitrate_mbps,
            "rtt_ms": rtt_ms,
            "loss": loss_eff,
        }
        u_task_hat = self.util_est.predict(feat)

        return {
            "bandwidth_mbps": bw,
            "rtt_ms": rtt_ms,
            "loss": loss_eff,
            "bitrate_mbps": bitrate_mbps,
            "delay_ms": delay_ms,
            "qp_base": qp_base,
            "delta_qp_roi": delta_qp_roi,
            "roi_area": roi_area,
            "obj_count": obj_count,
            "mean_conf": mean_conf,
            "motion": motion,
            "u_task_hat": u_task_hat,
            "u_task_gt": u_task_gt,
            "end_of_video": False,
        }

    def _frames_for_current_video(self):
        vid = self._video_ids[self.video_ix % len(self._video_ids)]
        return self._frames_per_video.get(vid, self.FRAMES_PER_VIDEO)

    def _advance_indices(self):
        """Advance frame/video pointers after one step. Set end_of_video on video boundary."""
        end_of_video = False
        self.frame_ix += 1
        if self.frame_ix >= self._frames_for_current_video():
            end_of_video = True
            self.frame_ix = 0
            self.video_ix += 1
            if len(self.all_cooked_bw) > 0:
                self._episode_trace_snippet = int(self.rng.randint(0, len(self.all_cooked_bw)))
            self.trace_pkt_idx = self.rng.randint(0, 1000)
        return end_of_video

    def current_decision_features(self):
        """Pre-action observation used to DECIDE the encoding action.

        Returns the current frame's *content* features and the current network
        condition, both available BEFORE the action is chosen:

          - Content (roi_area, obj_count, mean_conf, motion) is identical across
            all 20 actions of a frame (computed from the uncompressed frame), so
            exposing it pre-encode is realistic for a VCM controller.
          - Network (bandwidth, rtt, loss) is the condition that will apply to
            this step (pre-sampled in _step_*).
          - qp_base / bitrate_mbps carry the PREVIOUS applied action as history.

        This fixes the causal misalignment where the agent previously decided an
        action for frame t+1 using frame t's (independent) observation, which made
        the state statistically uninformative and forced policy collapse.
        """
        vid, fid = self._current_vid_fid()
        qb0 = cfg.QP_BASE_SET[0]
        dr0 = cfg.ROI_QP_OFFSET_SET[0]
        key = (vid, fid, int(qb0), int(dr0))
        if key not in self._lookup:
            raise KeyError("Missing profile row for decision features %s" % (key,))
        row = self._lookup[key]
        return {
            "bandwidth_mbps": float(self._step_bw),
            "rtt_ms": float(self._step_rtt),
            "loss": float(self._step_loss),
            "qp_base": int(self._last_qp),
            "bitrate_mbps": float(self._last_bitrate),
            "roi_area": float(row["roi_area"]),
            "obj_count": int(row["obj_count"]),
            "mean_conf": float(row["mean_conf"]),
            "motion": float(row["motion"]),
        }

    def get_video_chunk(self, qp_base, delta_qp_roi):
        vid, fid = self._current_vid_fid()
        key = (vid, fid, int(qp_base), int(delta_qp_roi))
        if key not in self._lookup:
            raise KeyError("Missing profile row for %s" % (key,))

        row = self._lookup[key]
        # Use the pre-sampled step-level network conditions (consistent for all
        # 20 actions at this step; avoids reward noise from per-action bandwidth).
        obs = self._row_to_obs(
            row,
            self._step_bw,
            rtt_ms_override=self._step_rtt,
            loss_override=self._step_loss,
        )

        # Remember this action's outcome as history for the NEXT decision state.
        self._last_qp = int(qp_base)
        self._last_bitrate = float(obs["bitrate_mbps"])

        # Advance trace cursor BEFORE resampling so the next step gets the next
        # trace point (relevant only when trace is loaded).
        if self._trace_bw_at_cursor() is not None:
            self._advance_trace_cursor()

        obs["end_of_video"] = self._advance_indices()

        # Pre-sample network conditions for the NEXT step.
        self._step_bw, self._step_rtt, self._step_loss = self._draw_step_network()
        return obs

    def peek_action_observations(self):
        """
        For oracle / debugging: all action outcomes at the current (video, frame) without advancing.
        All 20 actions use the SAME step-level network conditions so scores are comparable.
        """
        vid, fid = self._current_vid_fid()
        out = []
        for aid in range(cfg.A_DIM):
            qb, dr = cfg.decode_action(aid)
            key = (vid, fid, int(qb), int(dr))
            if key not in self._lookup:
                raise KeyError("peek missing profile row for %s" % (key,))
            row = self._lookup[key]
            out.append(self._row_to_obs(
                row,
                self._step_bw,
                rtt_ms_override=self._step_rtt,
                loss_override=self._step_loss,
            ))
        return out

    def reset_episode(self):
        """Resample trace snippet; optional call between test episodes."""
        self.video_ix = 0
        self.frame_ix = 0
        if len(self.all_cooked_bw) > 0:
            self._episode_trace_snippet = int(self.rng.randint(0, len(self.all_cooked_bw)))
            self.trace_pkt_idx = int(self.rng.randint(0, 5000))
        else:
            self.trace_pkt_idx = 0
        self._step_bw, self._step_rtt, self._step_loss = self._draw_step_network()
        self._last_qp = int(np.median(cfg.QP_BASE_SET))
        self._last_bitrate = float(cfg.MAX_BITRATE_MBPS) * 0.3


def get_state_vector(obs, prev_obs=None):
    """Normalized 9-D state vector for one timestep (Palette-style history assembled in trainer)."""
    del prev_obs  # Reserved for temporal deltas / richer task features.
    bw = float(obs["bandwidth_mbps"]) / cfg.MAX_BANDWIDTH_MBPS
    rt = float(obs["rtt_ms"]) / cfg.MAX_RTT_MS
    ls = float(np.clip(obs["loss"], 0.0, 1.0))
    denom_qp = max(float(cfg.QP_MAX - cfg.QP_MIN), 1e-6)
    qp_n = (float(obs["qp_base"]) - float(cfg.QP_MIN)) / denom_qp
    br_n = float(obs["bitrate_mbps"]) / cfg.MAX_BITRATE_MBPS
    roi = float(np.clip(obs["roi_area"], 0.0, 1.0))
    oc = float(obs["obj_count"]) / cfg.MAX_OBJ_COUNT
    mc = float(np.clip(obs["mean_conf"], 0.0, 1.0))
    mo = float(obs["motion"]) / max(float(cfg.MAX_MOTION), 1e-6)
    vec = np.array([bw, rt, ls, qp_n, br_n, roi, oc, mc, mo], dtype=np.float32)
    return vec
