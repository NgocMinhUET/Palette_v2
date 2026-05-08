# coding=utf-8
"""
Aggregate results from vcm/test_vcm.py output (test_results.csv).
"""

from __future__ import print_function

import argparse
import csv
import os
import sys


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_repo_path(p):
    if os.path.isabs(p):
        return p
    return os.path.normpath(os.path.join(_REPO_ROOT, p))


def load_rows(path):
    rows = []
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def summarize(rows):
    """Keep latest row per policy if multiple runs exist."""
    latest = {}
    for r in rows:
        latest[r["policy"]] = r
    out = []
    for pol, r in sorted(latest.items()):
        br = float(r["avg_bitrate"])
        ut = float(r["avg_u_task_gt"])
        out.append(
            {
                "policy": pol,
                "avg_u_task_gt": ut,
                "avg_u_task_hat": float(r["avg_u_task_hat"]),
                "avg_bitrate": br,
                "avg_delay": float(r["avg_delay"]),
                "avg_reward": float(r["avg_reward"]),
                "task_per_bitrate": ut / br if br > 1e-9 else 0.0,
            }
        )
    return out


def print_table(entries):
    cols = [
        "policy",
        "avg_u_task_gt",
        "avg_u_task_hat",
        "avg_bitrate",
        "avg_delay",
        "avg_reward",
        "task_per_bitrate",
    ]
    header = "\t".join(cols)
    print(header)
    for e in entries:
        print("\t".join(str(e[c]) for c in cols))


def parse_args():
    ap = argparse.ArgumentParser(description="Summarize CL-ROI-VCM test_results.csv")
    ap.add_argument(
        "--results_csv",
        type=str,
        default="results/vcm_logs/test_results.csv",
        help="CSV produced by test_vcm.py",
    )
    return ap.parse_args()


def main():
    args = parse_args()
    path = _resolve_repo_path(args.results_csv)
    if not os.path.isfile(path):
        print("No results file at %s — run test_vcm.py first." % path)
        sys.exit(1)
    rows = load_rows(path)
    entries = summarize(rows)
    print_table(entries)


if __name__ == "__main__":
    main()
