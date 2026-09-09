"""Cross-metric incident clustering.

A real operational incident rarely announces itself on a single metric.
A tablespace filling up shows up as elevated connection-pool utilization
*and*, a little later, as slower DAG tasks. Triaging each anomalous
interval independently would flood an on-call engineer with redundant
alerts and would also starve the retrieval step of the cross-metric
evidence a runbook match actually needs.

This module merges anomalous intervals from `anomaly_detection` into
"incidents" using a simple union-find over an overlap-with-tolerance
relation: two intervals belong to the same incident if their time spans
are within `correlation_window` of each other. This is a standard
approach for event correlation in observability systems and keeps the
logic auditable (no learned model, no hidden state).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.anomaly_detection import DetectionResult, anomalous_intervals


@dataclass
class Incident:
    incident_id: int
    start: pd.Timestamp
    end: pd.Timestamp
    intervals: list[dict] = field(default_factory=list)

    @property
    def metrics(self) -> list[str]:
        return sorted({iv["metric"] for iv in self.intervals})

    @property
    def services(self) -> list[str]:
        return sorted({iv["service"] for iv in self.intervals})

    @property
    def peak_severity(self) -> float:
        return max(iv["peak_abs_modified_z"] for iv in self.intervals)

    def evidence_summary(self) -> str:
        """Human/LLM-readable description of the incident's evidence, used
        as the retrieval query and as grounding context for triage."""
        parts = []
        for iv in sorted(self.intervals, key=lambda x: x["start"]):
            duration_h = max(int((iv["end"] - iv["start"]).total_seconds() // 3600), 1)
            parts.append(
                f"{iv['metric']} on service '{iv['service']}' deviated abnormally "
                f"for about {duration_h}h starting {iv['start']} "
                f"(peak robust z-score {iv['peak_abs_modified_z']:.1f})"
            )
        return "; ".join(parts)

    def as_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "metrics": self.metrics,
            "services": self.services,
            "peak_severity": self.peak_severity,
            "evidence_summary": self.evidence_summary(),
            "intervals": [
                {**iv, "start": iv["start"].isoformat(), "end": iv["end"].isoformat()}
                for iv in self.intervals
            ],
        }


def _overlaps_within(a: dict, b: dict, tolerance: pd.Timedelta) -> bool:
    a_start, a_end = a["start"] - tolerance, a["end"] + tolerance
    return a_start <= b["end"] and b["start"] <= a_end


def cluster_incidents(
    detection_results: list[DetectionResult],
    correlation_window_hours: int = 3,
    merge_gap_hours: int = 2,
) -> list[Incident]:
    all_intervals: list[dict] = []
    for result in detection_results:
        all_intervals.extend(anomalous_intervals(result, merge_gap_hours=merge_gap_hours))

    if not all_intervals:
        return []

    all_intervals.sort(key=lambda iv: iv["start"])
    tolerance = pd.Timedelta(hours=correlation_window_hours)

    # Union-find over intervals connected by the overlap-with-tolerance relation.
    parent = list(range(len(all_intervals)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(len(all_intervals)):
        for j in range(i + 1, len(all_intervals)):
            if _overlaps_within(all_intervals[i], all_intervals[j], tolerance):
                union(i, j)

    groups: dict[int, list[dict]] = {}
    for idx, iv in enumerate(all_intervals):
        groups.setdefault(find(idx), []).append(iv)

    incidents = []
    for incident_id, (_, intervals) in enumerate(
        sorted(groups.items(), key=lambda kv: min(iv["start"] for iv in kv[1])), start=1
    ):
        incidents.append(Incident(
            incident_id=incident_id,
            start=min(iv["start"] for iv in intervals),
            end=max(iv["end"] for iv in intervals),
            intervals=intervals,
        ))
    return incidents


if __name__ == "__main__":
    from src.data_synthesis import generate_dataset
    from src.anomaly_detection import detect_all

    ds = generate_dataset()
    results = detect_all(ds.frame)
    incidents = cluster_incidents(results)
    print(f"{len(incidents)} incident(s) clustered from {sum(len(anomalous_intervals(r)) for r in results)} raw intervals\n")
    for inc in incidents:
        print(f"Incident #{inc.incident_id}: {inc.start} -> {inc.end}")
        print(f"  metrics: {inc.metrics}")
        print(f"  evidence: {inc.evidence_summary()}\n")
