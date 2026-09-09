import numpy as np
import pandas as pd

from src.anomaly_detection import anomalous_intervals, detect_metric, modified_z_score


def _make_seasonal_series(n_hours: int = 24 * 14, spike_hour_index: int = 200,
                           spike_magnitude: float = 60.0, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2026-01-01", periods=n_hours, freq="h")
    hours = timestamps.hour.values.astype(float)
    seasonal = 20 * (1 + np.sin(2 * np.pi * hours / 24 - np.pi / 2)) / 2
    noise = rng.normal(0, 4, size=n_hours)
    values = 100 + seasonal + noise
    values[spike_hour_index:spike_hour_index + 3] += spike_magnitude
    return pd.DataFrame({
        "timestamp": timestamps,
        "metric": "test_metric",
        "service": "test-service",
        "value": values,
    })


def test_modified_z_score_flags_obvious_outlier():
    residual = np.concatenate([np.zeros(50), [50.0]])
    z = modified_z_score(residual)
    assert abs(z[-1]) > 10
    assert np.all(np.abs(z[:-1]) < 1)


def test_detect_metric_finds_injected_spike():
    frame = _make_seasonal_series()
    result = detect_metric(frame, period=24)

    spike_window = result.frame.iloc[200:203]
    assert spike_window["is_anomaly"].any(), "Injected spike was not detected"

    # Away from the spike, the false-positive rate should be low.
    baseline = result.frame.drop(result.frame.index[195:210])
    false_positive_rate = baseline["is_anomaly"].mean()
    assert false_positive_rate < 0.05


def test_anomalous_intervals_merge_adjacent_points():
    frame = _make_seasonal_series(spike_hour_index=300, spike_magnitude=80.0)
    result = detect_metric(frame, period=24)
    intervals = anomalous_intervals(result, merge_gap_hours=2)

    assert len(intervals) >= 1
    matching = [iv for iv in intervals if iv["start"] <= frame.loc[301, "timestamp"] <= iv["end"]]
    assert matching, "Expected an interval covering the injected spike"
