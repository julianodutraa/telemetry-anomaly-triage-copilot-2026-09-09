from src.retrieval import RunbookRetriever
from src.runbook_corpus import load_corpus


def test_corpus_loads_expected_runbooks():
    runbooks = load_corpus()
    ids = {rb.id for rb in runbooks}
    assert "db-connection-pool-saturation" in ids
    assert "stream-consumer-backpressure" in ids
    assert "dag-task-duration-spike" in ids
    assert "replication-watcher-stall" in ids
    assert len(runbooks) >= 6, "Expect at least 4 primary runbooks plus distractors"


def test_snake_case_metric_names_are_normalized_for_matching():
    from src.retrieval import _normalize
    assert "db connection pool utilization pct" == _normalize("db_connection_pool_utilization_pct")


def test_retrieval_top1_matches_expected_runbook_for_each_archetype():
    retriever = RunbookRetriever()
    cases = [
        (
            "db_connection_pool_utilization_pct on service 'primary-db' deviated abnormally, "
            "gradually climbing and plateauing near its ceiling over several hours",
            "db-connection-pool-saturation",
        ),
        (
            "queue_depth_messages on service 'stream-consumer' deviated abnormally, rising "
            "steadily over several hours without draining back to baseline",
            "stream-consumer-backpressure",
        ),
        (
            "dag_task_duration_seconds on service 'ingestion-dag' deviated abnormally for a short, "
            "bounded window before returning to normal on its own",
            "dag-task-duration-spike",
        ),
        (
            "replication_lag_seconds on service 'replica-db' deviated abnormally, collapsing toward "
            "zero and flatlining there",
            "replication-watcher-stall",
        ),
    ]
    for query, expected_id in cases:
        top = retriever.retrieve(query, top_k=1)[0]
        assert top.runbook.id == expected_id, f"Expected {expected_id}, got {top.runbook.id} for query: {query}"
