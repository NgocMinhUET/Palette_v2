# coding=utf-8
"""
Task utility estimator: online u_task_hat in [0, 1].

mAP is not used online — only heuristic or offline-trained regression from profiles.
"""

import csv
import os
import pickle

import numpy as np

import vcm_config as cfg


def _heuristic_u(features_dict):
    mean_conf = float(features_dict.get("mean_conf", 0.5))
    qp_base = float(features_dict.get("qp_base", 28))
    delta_qp_roi = float(features_dict.get("delta_qp_roi", 0))
    motion = float(features_dict.get("motion", 0.0))
    loss = float(features_dict.get("loss", 0.0))
    bitrate_mbps = float(features_dict.get("bitrate_mbps", 2.0))

    u = mean_conf
    u -= 0.015 * (qp_base - 24)
    u += 0.02 * max(0.0, -delta_qp_roi)
    u -= 0.001 * motion
    u -= 0.1 * loss
    u -= 0.01 * max(0.0, bitrate_mbps - 5)
    return float(np.clip(u, 0.0, 1.0))


class TaskUtilityEstimator(object):
    """
    Default: heuristic. Optional: RandomForestRegressor / MLPRegressor via sklearn.
    """

    def __init__(self):
        self._mode = "heuristic"
        self._model = None

    def fit_from_csv(self, csv_path):
        """Train regressor from offline profile if sklearn is available."""
        try:
            from sklearn.ensemble import RandomForestRegressor
            from sklearn.neural_network import MLPRegressor
        except ImportError:
            self._mode = "heuristic"
            self._model = None
            return False

        rows_x = []
        rows_y = []
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                x = [
                    float(row["roi_area"]),
                    float(row["obj_count"]),
                    float(row["mean_conf"]),
                    float(row["motion"]),
                    float(row["qp_base"]),
                    float(row["delta_qp_roi"]),
                    float(row["bitrate_mbps"]),
                    float(row["rtt_ms"]),
                    float(row["loss"]),
                ]
                rows_x.append(x)
                rows_y.append(float(row["u_task_gt"]))

        X = np.asarray(rows_x, dtype=np.float32)
        y = np.asarray(rows_y, dtype=np.float32)
        if len(y) < 50:
            self._mode = "heuristic"
            self._model = None
            return False

        try:
            self._model = RandomForestRegressor(
                n_estimators=50, max_depth=12, random_state=0, n_jobs=-1
            )
            self._model.fit(X, y)
            self._mode = "sklearn_rf"
        except Exception:
            self._model = MLPRegressor(hidden_layer_sizes=(64, 32), max_iter=300, random_state=0)
            self._model.fit(X, y)
            self._mode = "sklearn_mlp"

        return True

    def predict(self, features_dict):
        """Return u_task_hat in [0, 1]."""
        if self._mode == "heuristic" or self._model is None:
            return _heuristic_u(features_dict)

        x = np.array(
            [
                [
                    float(features_dict.get("roi_area", 0)),
                    float(features_dict.get("obj_count", 0)),
                    float(features_dict.get("mean_conf", 0)),
                    float(features_dict.get("motion", 0)),
                    float(features_dict.get("qp_base", cfg.QP_MIN)),
                    float(features_dict.get("delta_qp_roi", 0)),
                    float(features_dict.get("bitrate_mbps", 0)),
                    float(features_dict.get("rtt_ms", 0)),
                    float(features_dict.get("loss", 0)),
                ]
            ],
            dtype=np.float32,
        )
        yhat = float(self._model.predict(x)[0])
        return float(np.clip(yhat, 0.0, 1.0))

    def save(self, path):
        payload = {"mode": self._mode, "model": self._model}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(payload, f)

    def load(self, path):
        with open(path, "rb") as f:
            payload = pickle.load(f)
        self._mode = payload.get("mode", "heuristic")
        self._model = payload.get("model", None)
        if self._model is None:
            self._mode = "heuristic"
