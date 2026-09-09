"""End-to-end orchestration: synthesize -> detect -> cluster -> retrieve -> triage -> evaluate.

Run as a script for a self-contained demo on synthetic data:

    python -m src.pipeline --backend stub --seed 42

See README.md for the full command reference and for how to point
`--backend openai` at a real LLM.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from src.anomaly_detection import detect_all
from src.data_synthesis import generate_dataset
from src.evaluation import score_detection, score_retrieval, score_triage, print_report
from src.incident_clustering import Incident, cluster_incidents
from src.llm_triage import LLMClient, TriageReport, get_default_client
from src.retrieval import RetrievedRunbook, RunbookRetriever


@dataclass
class PipelineRun:
    incidents: list[Incident]
    retrieved_by_incident: dict[int, list[RetrievedRunbook]]
    triage_reports: dict[int, TriageReport]


def run_pipeline(seed: int = 42, backend: str = "stub", top_k: int = 3) -> tuple[PipelineRun, object]:
    dataset = generate_dataset(seed=seed)
    detection_results = detect_all(dataset.frame)
    incidents = cluster_incidents(detection_results)

    retriever = RunbookRetriever()
    client: LLMClient = get_default_client(backend)

    retrieved_by_incident: dict[int, list[RetrievedRunbook]] = {}
    triage_reports: dict[int, TriageReport] = {}
    for incident in incidents:
        retrieved = retriever.retrieve(incident.evidence_summary(), top_k=top_k)
        retrieved_by_incident[incident.incident_id] = retrieved
        triage_reports[incident.incident_id] = client.triage(incident, retrieved)

    run = PipelineRun(incidents=incidents, retrieved_by_incident=retrieved_by_incident, triage_reports=triage_reports)
    return run, dataset


def evaluate_run(run: PipelineRun, dataset) -> dict:
    detection = score_detection(dataset.events, run.incidents)
    retrieval = score_retrieval(run.incidents, run.retrieved_by_incident, dataset.events)
    triage = score_triage(run.incidents, run.triage_reports, dataset.events)
    return {"detection": detection, "retrieval": retrieval, "triage": triage}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--backend", choices=["stub", "openai"], default="stub")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--output", type=str, default=None, help="Optional path to write a JSON report")
    args = parser.parse_args()

    run, dataset = run_pipeline(seed=args.seed, backend=args.backend, top_k=args.top_k)

    print(f"\nGenerated {len(dataset.events)} ground-truth incidents; "
          f"detector produced {len(run.incidents)} incident cluster(s).\n")
    for incident in run.incidents:
        report = run.triage_reports[incident.incident_id]
        print(f"--- Incident #{incident.incident_id} [{incident.start} -> {incident.end}] ---")
        print(f"Metrics involved : {', '.join(incident.metrics)}")
        print(f"Evidence         : {incident.evidence_summary()}")
        print(f"Root cause       : {report.root_cause_label}  (confidence: {report.confidence})")
        print(f"Explanation      : {report.explanation}")
        print(f"Recommended step : {report.recommended_action}")
        print(f"Backend          : {report.backend}\n")

    scores = evaluate_run(run, dataset)
    print_report(scores["detection"], scores["retrieval"], scores["triage"])

    if args.output:
        payload = {
            "incidents": [inc.as_dict() for inc in run.incidents],
            "triage_reports": {str(k): v.as_dict() for k, v in run.triage_reports.items()},
            "scores": {
                "detection": vars(scores["detection"]),
                "retrieval": vars(scores["retrieval"]),
                "triage": vars(scores["triage"]),
            },
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nFull report written to {args.output}")


if __name__ == "__main__":
    main()
