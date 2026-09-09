"""Robust statistical anomaly detection for seasonal telemetry series.

Method
------
1. Seasonal-Trend decomposition using LOESS (STL, Cleveland et al. 1990)
   separates each metric into trend, daily-seasonal, and residual
   components. Detecting anomalies on the residual, rather than on the
   raw value, avoids flagging ordinary daily peaks (e.g. business-hours
   queue depth) as incidents.
2. The residual is scored with the modified z-score of Iglewicz and
   Hoaglin (1993), which uses the median and the median absolute
   deviation (MAD) instead of the mean and standard deviation:

       modified_z_i = 0.6745 * (r_i - median(r)) / MAD(r)

   The 0.6745 constant makes the statistic comparable to a standard
   z-score under normality, and because it is based on medians it is
   far less sensitive to the very anomalies it is trying to detect than
   a plain (mean, std) z-score would be.
3. A point is flagged anomalous when |modified_z| exceeds a threshold
   (3.5 by default, the conventional Iglewicz-Hoaglin cutoff).

This is a standard, well-understood robust-statistics approach. It is
deliberately not a black box: every flagged point comes with a
reconstructable score, which matters for an on-call tool that has to
justify itself during an incident review.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from statsmodels.tsa.seasonal import STL

MODIFIED_Z_THRESHOLD = 3.5
MAD_EPSILON = 1e-9


@dataclass
class DetectionResult:
    metric: str
    service: str
    frame: pd.DataFrame  # columns: timestamp, value, trend, seasonal, resid, modified_z, is_anomaly


def modified_z_score(residual: np.ndarray) -> np.ndarray:
    median = np.median(residual)
    mad = np.median(np.abs(residual - median))
    return 0.6745 * (residual - median) / (mad + MAD_EPSILON)


def detect_metric(
    frame: pd.DataFrame,
    period: int = 24,
    threshold: float = MODIFIED_Z_THRESHOLD,
) -> DetectionResult:
    """Run STL decomposition + robust residual scoring on one metric's series.

    `frame` must be sorted by timestamp and contain a single metric/service
    pair, with columns `timestamp` and `value`.
    """
    metric = frame["metric"].iloc[0]
    service = frame["service"].iloc[0]
    series = frame.set_index("timestamp")["value"]

    # robust=False is used deliberately: STL's IRLS-based robustness
    # weighting is tuned for occasional gross outliers in the *trend* fit,
    # but on these hourly operational series it under-estimates the
    # residual MAD (verified empirically), which inflates the modified
    # z-score and produces excess false positives. A plain STL fit followed
    # by MAD-based scoring on its residual is the more calibrated combination
    # here; this is a good example of why every anomaly detector needs to be
    # validated against labeled data rather than trusted on defaults alone.
    stl = STL(series, period=period, robust=False)
    result = stl.fit()

    resid = result.resid.to_numpy()
    z = modified_z_score(resid)
    is_anomaly = np.abs(z) > threshold

    out = pd.DataFrame({
        "timestamp": series.index,
        "value": series.to_numpy(),
        "trend": result.trend.to_numpy(),
        "seasonal": result.seasonal.to_numpy(),
        "resid": resid,
        "modified_z": z,
        "is_anomaly": is_anomaly,
    })
    return DetectionResult(metric=metric, service=service, frame=out)


def detect_all(dataset_frame: pd.DataFrame, period: int = 24,
                threshold: float = MODIFIED_Z_THRESHOLD) -> list[DetectionResult]:
    results = []
    for (metric, service), group in dataset_frame.groupby(["metric", "service"], sort=False):
        sorted_group = group.sort_values("timestamp").reset_index(drop=True)
        results.append(detect_metric(sorted_group, period=period, threshold=threshold))
    return results


def anomalous_intervals(result: DetectionResult, merge_gap_hours: int = 2) -> list[dict]:
    """Collapse a boolean anomaly mask into contiguous intervals.

    Consecutive (or near-consecutive, within `merge_gap_hours`) anomalous
    timestamps are merged into a single interval so a 6-hour incident does
    not get reported as six separate one-hour anomalies.
    """
    frame = result.frame
    anomalous = frame[frame["is_anomaly"]].reset_index(drop=True)
    if anomalous.empty:
        return []

    intervals: list[dict] = []
    cur_start = anomalous.loc[0, "timestamp"]
    cur_end = anomalous.loc[0, "timestamp"]
    cur_scores = [anomalous.loc[0, "modified_z"]]

    gap = pd.Timedelta(hours=merge_gap_hours)
    for i in range(1, len(anomalous)):
        ts = anomalous.loc[i, "timestamp"]
        if ts - cur_end <= gap:
            cur_end = ts
            cur_scores.append(anomalous.loc[i, "modified_z"])
        else:
            intervals.append(_make_interval(result, cur_start, cur_end, cur_scores))
            cur_start, cur_end = ts, ts
            cur_scores = [anomalous.loc[i, "modified_z"]]
    intervals.append(_make_interval(result, cur_start, cur_end, cur_scores))
    return intervals


def _make_interval(result: DetectionResult, start: pd.Timestamp, end: pd.Timestamp,
                    scores: list[float]) -> dict:
    return {
        "metric": result.metric,
        "service": result.service,
        "start": start,
        "end": end,
        "peak_abs_modified_z": float(np.max(np.abs(scores))),
        "n_points": len(scores),
    }


if __name__ == "__main__":
    from src.data_synthesis import generate_dataset

    ds = generate_dataset()
    results = detect_all(ds.frame)
    for r in results:
        intervals = anomalous_intervals(r)
        print(f"{r.metric} ({r.service}): {len(intervals)} anomalous interval(s)")
        for iv in intervals:
            print("  ", iv)
