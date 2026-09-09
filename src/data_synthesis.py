"""Synthetic multi-metric telemetry generator for data-pipeline observability.

The generator produces hourly time series for four metrics that are
representative of the health signals a data platform team typically
watches: DAG task duration, consumer queue depth, database connection
pool utilization, and replica replication lag. Each series has a
realistic daily seasonal component plus noise, and a small number of
labeled anomaly events are injected so the rest of the pipeline can be
evaluated against known ground truth.

Everything here is fully synthetic. No real system, customer, or
employer telemetry is used or referenced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

RNG_DEFAULT_SEED = 42


@dataclass
class AnomalyEvent:
    """Ground-truth record of an injected anomaly."""

    metric: str
    service: str
    anomaly_type: str
    start: pd.Timestamp
    end: pd.Timestamp
    magnitude: float

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "service": self.service,
            "anomaly_type": self.anomaly_type,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "magnitude": self.magnitude,
        }


@dataclass
class MetricSpec:
    name: str
    service: str
    baseline: float
    daily_amplitude: float
    noise_std: float
    floor: float = 0.0


DEFAULT_METRICS = [
    MetricSpec("dag_task_duration_seconds", "ingestion-dag", baseline=120.0,
               daily_amplitude=25.0, noise_std=6.0, floor=5.0),
    MetricSpec("queue_depth_messages", "stream-consumer", baseline=800.0,
               daily_amplitude=300.0, noise_std=40.0, floor=0.0),
    MetricSpec("db_connection_pool_utilization_pct", "primary-db", baseline=45.0,
               daily_amplitude=10.0, noise_std=3.0, floor=0.0),
    MetricSpec("replication_lag_seconds", "replica-db", baseline=8.0,
               daily_amplitude=3.0, noise_std=1.5, floor=0.0),
]


@dataclass
class SyntheticDataset:
    frame: pd.DataFrame
    events: list[AnomalyEvent] = field(default_factory=list)


def _daily_seasonal(hours: np.ndarray, amplitude: float, phase: float = -np.pi / 2) -> np.ndarray:
    """24-hour seasonal component peaking mid-day."""
    return amplitude * (1 + np.sin(2 * np.pi * hours / 24 + phase)) / 2


def _apply_event(values: np.ndarray, idx_start: int, idx_end: int, anomaly_type: str,
                  magnitude: float, floor: float) -> np.ndarray:
    values = values.copy()
    span = max(idx_end - idx_start, 1)
    if anomaly_type == "transient_spike":
        # Short, sharp point anomaly (e.g. a retry storm or GC pause).
        values[idx_start:idx_end] += magnitude
    elif anomaly_type == "resource_exhaustion":
        # Linear ramp up to a sustained plateau within the labeled window
        # (e.g. a tablespace filling up until it is remediated).
        ramp = np.linspace(0.15 * magnitude, magnitude, span)
        values[idx_start:idx_end] += ramp
    elif anomaly_type == "capacity_saturation":
        # Gradual saturation that plateaus near the end of the window.
        ramp = magnitude * (1 - np.exp(-np.linspace(0, 4, span)))
        values[idx_start:idx_end] += ramp
    elif anomaly_type == "dependency_stall":
        # Value collapses toward the floor (e.g. an upstream dependency
        # stops producing, so a downstream queue drains to near zero).
        values[idx_start:idx_end] = floor + (values[idx_start:idx_end] - floor) * 0.05
    else:
        raise ValueError(f"Unknown anomaly_type: {anomaly_type}")
    return np.clip(values, floor, None)


def generate_dataset(
    start: str = "2026-08-01T00:00:00",
    n_hours: int = 24 * 30,
    metrics: list[MetricSpec] | None = None,
    seed: int = RNG_DEFAULT_SEED,
    n_incidents: int = 4,
) -> SyntheticDataset:
    """Generate a synthetic multi-metric telemetry dataset with injected incidents.

    Each injected incident perturbs one primary metric with a distinct
    anomaly archetype. Real production incidents rarely move a single
    signal in isolation, so a small correlated nudge is also applied to
    one secondary metric for half of the incidents, which is what makes
    the downstream incident-clustering step meaningful.
    """
    metrics = metrics or DEFAULT_METRICS
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp(start)
    timestamps = pd.date_range(t0, periods=n_hours, freq="h")
    hours_of_day = timestamps.hour.values.astype(float)

    series: dict[str, np.ndarray] = {}
    for spec in metrics:
        seasonal = _daily_seasonal(hours_of_day, spec.daily_amplitude)
        noise = rng.normal(0, spec.noise_std, size=n_hours)
        series[spec.name] = np.clip(spec.baseline + seasonal + noise, spec.floor, None)

    anomaly_types = ["transient_spike", "resource_exhaustion", "capacity_saturation", "dependency_stall"]
    events: list[AnomalyEvent] = []

    # Space incidents out across the timeline, leaving a healthy margin at
    # the edges so STL decomposition always sees stable seasonal context.
    candidate_starts = rng.choice(
        np.arange(48, n_hours - 48), size=n_incidents, replace=False
    )
    candidate_starts.sort()

    for i, start_idx in enumerate(candidate_starts):
        spec = metrics[i % len(metrics)]
        anomaly_type = anomaly_types[i % len(anomaly_types)]
        duration = int(rng.integers(3, 10))
        end_idx = int(start_idx + duration)
        magnitude = float(rng.uniform(2.5, 4.5) * spec.noise_std + spec.daily_amplitude * 0.6)

        series[spec.name] = _apply_event(
            series[spec.name], int(start_idx), end_idx, anomaly_type, magnitude, spec.floor
        )
        events.append(AnomalyEvent(
            metric=spec.name, service=spec.service, anomaly_type=anomaly_type,
            start=timestamps[int(start_idx)], end=timestamps[min(end_idx, n_hours - 1)],
            magnitude=magnitude,
        ))

        # Correlated secondary signal for every other incident: a real
        # resource-exhaustion or saturation event on one service usually
        # produces a smaller ripple on an adjacent service.
        if i % 2 == 0:
            secondary = metrics[(i + 1) % len(metrics)]
            ripple_magnitude = magnitude * 0.35
            series[secondary.name] = _apply_event(
                series[secondary.name], int(start_idx), end_idx, "transient_spike",
                ripple_magnitude, secondary.floor,
            )

    frames = []
    for spec in metrics:
        frames.append(pd.DataFrame({
            "timestamp": timestamps,
            "metric": spec.name,
            "service": spec.service,
            "value": series[spec.name],
        }))
    frame = pd.concat(frames, ignore_index=True).sort_values(["metric", "timestamp"]).reset_index(drop=True)
    return SyntheticDataset(frame=frame, events=events)


if __name__ == "__main__":
    ds = generate_dataset()
    print(ds.frame.groupby("metric")["value"].describe())
    print("\nInjected ground-truth events:")
    for e in ds.events:
        print(e.as_dict())
