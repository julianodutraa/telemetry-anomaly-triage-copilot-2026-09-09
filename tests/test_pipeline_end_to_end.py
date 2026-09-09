from src.llm_triage import DeterministicStubClient
from src.pipeline import evaluate_run, run_pipeline


def test_pipeline_runs_end_to_end_and_meets_quality_bar():
    run, dataset = run_pipeline(seed=42, backend="stub")

    assert len(run.incidents) > 0
    assert len(dataset.events) == 4

    scores = evaluate_run(run, dataset)

    # Every injected incident should be detected (allowing imprecise
    # boundaries, since this is range-based recall).
    assert scores["detection"].range_recall == 1.0
    # Precision need not be perfect (a couple of single-point false
    # positives are expected from an untuned threshold), but the detector
    # should not be mostly noise.
    assert scores["detection"].range_precision >= 0.4

    # Retrieval and triage should get every incident right in this
    # deterministic, seeded configuration.
    assert scores["retrieval"].recall_at_1 == 1.0
    assert scores["triage"].accuracy == 1.0


def test_deterministic_stub_is_reproducible_across_runs():
    run_a, dataset_a = run_pipeline(seed=123, backend="stub")
    run_b, dataset_b = run_pipeline(seed=123, backend="stub")

    labels_a = [run_a.triage_reports[i.incident_id].root_cause_label for i in run_a.incidents]
    labels_b = [run_b.triage_reports[i.incident_id].root_cause_label for i in run_b.incidents]
    assert labels_a == labels_b


def test_every_triage_report_cites_a_real_runbook_or_is_explicitly_unmatched():
    run, _ = run_pipeline(seed=7, backend="stub")
    valid_ids = set()
    for retrieved in run.retrieved_by_incident.values():
        valid_ids.update(r.runbook.id for r in retrieved)

    for report in run.triage_reports.values():
        assert report.root_cause_label == "unmatched" or report.root_cause_label in valid_ids
        for cited in report.cited_runbook_ids:
            assert cited in valid_ids, "Triage cited a runbook id that was never retrieved"


def test_low_signal_evidence_is_reported_as_unmatched_not_guessed():
    from src.incident_clustering import Incident

    client = DeterministicStubClient()
    fake_incident = Incident(incident_id=999, start=None, end=None, intervals=[{
        "metric": "some_never_before_seen_metric",
        "service": "unknown-service",
        "start": None, "end": None,
        "peak_abs_modified_z": 3.6, "n_points": 1,
    }])
    fake_incident.evidence_summary = lambda: "a completely unrelated novel failure mode with no keyword overlap"
    report = client.triage(fake_incident, retrieved=[])
    assert report.root_cause_label == "unmatched"
    assert report.cited_runbook_ids == []
