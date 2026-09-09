"""Evaluation harness against synthetic ground truth.

Three things are measured, each against a metric with a real citation
rather than an ad hoc score:

1. **Detection quality**: range-based precision and recall between the
   detector's anomalous intervals and the injected ground-truth
   anomaly windows, following the existence-based variant of Tatbul et
   al., "Precision and Recall for Time Series" (NeurIPS 2018). Point-
   wise precision/recall is a poor fit for time-series anomalies
   because it over-penalizes a detector that finds the right multi-hour
   event but with slightly different boundaries; range-based scoring
   credits any overlap with the true window.
2. **Retrieval quality**: recall@1 and recall@3 of the RAG retriever
   against a hand-authored ground-truth mapping from injected anomaly
   type to the runbook that was written to describe it.
3. **Root-cause triage accuracy**: whether the LLM client's top-line
   `root_cause_label` matches that same ground-truth runbook id.

All three are reported together because they answer different
questions a platform team actually has: "did we detect the incident at
all," "did we retrieve the right knowledge," and "did the triage layer
correctly use what was retrieved." A system can fail at any one of
these independently, and conflating them into a single end-to-end
accuracy number would hide which stage needs work.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.data_synthesis import AnomalyEvent
from src.incident_clustering import Incident
from src.llm_triage import TriageReport
from src.retrieval import RetrievedRunbook

# Ground truth mapping from injected anomaly type to the runbook that was
# written to describe it. This is domain knowledge established when the
# runbook corpus and the synthetic anomaly archetypes were designed
# together; it plays the same role a human-labeled eval set plays for a
# real production RAG system.
ANOMALY_TYPE_TO_RUNBOOK_ID = {
    "transient_spike": "dag-task-duration-spike",
    "resource_exhaustion": "stream-consumer-backpressure",
    "capacity_saturation": "db-connection-pool-saturation",
    "dependency_stall": "replication-watcher-stall",
}


@dataclass
class DetectionScore:
    range_precision: float
    range_recall: float
    range_f1: float
    n_true_events: int
    n_predicted_intervals: int


def _interval_overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start <= b_end and b_start <= a_end


def score_detection(
    true_events: list[AnomalyEvent],
    predicted_incidents: list[Incident],
    tolerance: pd.Timedelta = pd.Timedelta(hours=1),
) -> DetectionScore:
    """Existence-based range precision/recall (simplified Tatbul et al. 2018).

    Recall: fraction of true events that overlap at least one predicted
    interval (on the same metric, within `tolerance`).
    Precision: fraction of predicted intervals that overlap at least one
    true event (on the same metric, within `tolerance`).
    """
    predicted_intervals = [
        {"metric": iv["metric"], "start": iv["start"], "end": iv["end"]}
        for incident in predicted_incidents
        for iv in incident.intervals
    ]

    true_hits = 0
    for event in true_events:
        matched = any(
            pred["metric"] == event.metric
            and _interval_overlaps(event.start - tolerance, event.end + tolerance, pred["start"], pred["end"])
            for pred in predicted_intervals
        )
        true_hits += int(matched)

    pred_hits = 0
    for pred in predicted_intervals:
        matched = any(
            pred["metric"] == event.metric
            and _interval_overlaps(event.start - tolerance, event.end + tolerance, pred["start"], pred["end"])
            for event in true_events
        )
        pred_hits += int(matched)

    recall = true_hits / len(true_events) if true_events else 1.0
    precision = pred_hits / len(predicted_intervals) if predicted_intervals else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return DetectionScore(
        range_precision=precision,
        range_recall=recall,
        range_f1=f1,
        n_true_events=len(true_events),
        n_predicted_intervals=len(predicted_intervals),
    )


@dataclass
class RetrievalScore:
    recall_at_1: float
    recall_at_3: float
    n_evaluated: int


def score_retrieval(
    incidents: list[Incident],
    retrieved_by_incident: dict[int, list[RetrievedRunbook]],
    true_events: list[AnomalyEvent],
) -> RetrievalScore:
    """Recall@1 / Recall@3 against the expected runbook for each incident's
    dominant (highest-severity) metric."""
    hits_at_1, hits_at_3, n = 0, 0, 0
    for incident in incidents:
        expected_id = _expected_runbook_for_incident(incident, true_events)
        if expected_id is None:
            continue
        n += 1
        retrieved_ids = [r.runbook.id for r in retrieved_by_incident.get(incident.incident_id, [])]
        if retrieved_ids[:1] == [expected_id]:
            hits_at_1 += 1
        if expected_id in retrieved_ids[:3]:
            hits_at_3 += 1
    return RetrievalScore(
        recall_at_1=hits_at_1 / n if n else 0.0,
        recall_at_3=hits_at_3 / n if n else 0.0,
        n_evaluated=n,
    )


def _expected_runbook_for_incident(incident: Incident, true_events: list[AnomalyEvent]) -> str | None:
    """Match an incident to the ground-truth event with the most temporal
    overlap, then look up that event's expected runbook."""
    best_event, best_overlap = None, pd.Timedelta(0)
    for event in true_events:
        if event.metric not in incident.metrics:
            continue
        overlap_start = max(event.start, incident.start)
        overlap_end = min(event.end, incident.end)
        overlap = max(overlap_end - overlap_start, pd.Timedelta(0))
        if overlap >= best_overlap:
            best_overlap, best_event = overlap, event
    if best_event is None:
        return None
    return ANOMALY_TYPE_TO_RUNBOOK_ID.get(best_event.anomaly_type)


@dataclass
class TriageScore:
    accuracy: float
    n_evaluated: int


def score_triage(
    incidents: list[Incident],
    triage_reports: dict[int, TriageReport],
    true_events: list[AnomalyEvent],
) -> TriageScore:
    correct, n = 0, 0
    for incident in incidents:
        expected_id = _expected_runbook_for_incident(incident, true_events)
        if expected_id is None:
            continue
        n += 1
        report = triage_reports.get(incident.incident_id)
        if report is not None and report.root_cause_label == expected_id:
            correct += 1
    return TriageScore(accuracy=correct / n if n else 0.0, n_evaluated=n)


def print_report(detection: DetectionScore, retrieval: RetrievalScore, triage: TriageScore) -> None:
    print("=" * 72)
    print("EVALUATION SUMMARY")
    print("=" * 72)
    print(f"Detection  (range-based, Tatbul et al. 2018 style):")
    print(f"  precision={detection.range_precision:.2f}  recall={detection.range_recall:.2f}  "
          f"f1={detection.range_f1:.2f}  "
          f"(true_events={detection.n_true_events}, predicted_intervals={detection.n_predicted_intervals})")
    print(f"Retrieval  (against expected runbook per incident):")
    print(f"  recall@1={retrieval.recall_at_1:.2f}  recall@3={retrieval.recall_at_3:.2f}  "
          f"(n={retrieval.n_evaluated})")
    print(f"Triage     (root-cause label matches expected runbook id):")
    print(f"  accuracy={triage.accuracy:.2f}  (n={triage.n_evaluated})")
    print("=" * 72)
