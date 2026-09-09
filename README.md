# Telemetry Anomaly Triage Copilot

A retrieval-augmented, LLM-assisted root-cause triage pipeline for data-pipeline and infrastructure observability telemetry, built and evaluated end to end on synthetic data.

## Executive summary

On-call engineers on a data platform team spend a large share of every incident's duration not fixing anything, but figuring out *which* known failure mode a set of correlated dashboard alerts most likely represents. That triage step is manual, inconsistent across engineers, and does not scale with the number of metrics a modern pipeline emits. As pipelines grow (more DAGs, more services, more databases, more Kubernetes workloads), the number of dashboards someone has to mentally cross-reference during an incident grows with it, while mean time to resolution (MTTR) is exactly the metric leadership cares most about during an outage.

This project is a working, evaluated prototype of a system that automates that first triage step. It statistically detects anomalies across multiple correlated telemetry metrics, groups them into a single incident, retrieves the most relevant known failure-mode documentation ("runbooks") for that incident's specific evidence, and uses an LLM to produce a structured, cited root-cause hypothesis and a first recommended action, in seconds rather than the minutes to tens of minutes a manual dashboard review takes.

The business case for this pattern rests on three points that matter to a technology leader evaluating whether to invest further in it:

1. **It targets time-to-hypothesis, not time-to-fix.** It does not claim to resolve incidents; it claims to shorten the diagnostic phase that precedes resolution, which is the phase most sensitive to on-call experience and to how many systems a person has to check by hand.
2. **It is grounded, not generative.** Every hypothesis the system produces is required to cite a specific, human-authored runbook. If the retrieved evidence does not clearly support any known failure mode, the system says so explicitly ("unmatched") rather than fabricating a plausible-sounding but ungrounded explanation. This is the property that makes an LLM-based tool defensible for production on-call use, where a confident hallucination is worse than no suggestion at all.
3. **It is measured against ground truth, not demoed on cherry-picked examples.** The evaluation harness described below reports detection recall/precision, retrieval recall@k, and triage accuracy against a labeled synthetic dataset, so the claim "this works" is backed by numbers that would hold up in a technical review, not just a favorable-looking transcript.

The remainder of this document is the technical deep dive: the statistical method behind detection, the retrieval-augmented generation (RAG) design, the LLM integration and grounding strategy, the evaluation methodology, and how to run and extend the project.

No proprietary systems, employer infrastructure, or confidential data are referenced anywhere in this repository. All telemetry is synthetically generated and all "services" named in the code are generic placeholders (`ingestion-dag`, `stream-consumer`, `primary-db`, `replica-db`).

## Problem framing

A data platform typically exposes several classes of health signal: orchestration metrics (DAG or job task duration), streaming/queue metrics (consumer lag, queue depth), and database metrics (connection pool utilization, replication lag). A single incident, such as a database connection leak, usually shows up gradually on one metric and only later ripples into others. An engineer paging in mid-incident has to notice the anomaly, recognize its shape, recall which of dozens of runbooks might apply, and read enough of it to know the first action to take. This project automates that sequence:

```
synthetic multi-metric        STL decomposition +      cross-metric        RAG retrieval over      LLM triage
telemetry (or real            robust MAD z-score  -->  incident      -->   a runbook corpus   -->  (grounded,
telemetry, swappable)    -->  anomaly detection         clustering          (TF-IDF by default)     structured JSON)
```

Each stage is a separate, independently testable module (`src/`), and each stage's output is inspectable, which matters for an operations tool that has to be auditable during an incident postmortem.

## Method

### 1. Statistical anomaly detection (`src/anomaly_detection.py`)

Each metric's hourly time series is decomposed with **STL (Seasonal-Trend decomposition using LOESS)**, separating it into trend, daily-seasonal, and residual components (Cleveland et al., 1990). Detecting anomalies on the residual, rather than on the raw value, is what keeps the detector from flagging an ordinary daily peak (business-hours queue depth, say) as an incident.

The residual is scored with the **modified z-score of Iglewicz and Hoaglin (1993)**, which substitutes the median and median absolute deviation (MAD) for the mean and standard deviation:

```
modified_z_i = 0.6745 * (r_i - median(r)) / MAD(r)
```

Using the median and MAD instead of the mean and standard deviation matters specifically because the statistic is used to detect outliers: a mean/std-based z-score is itself distorted by the very outliers it is trying to find, while the median and MAD stay stable in their presence. A point is flagged anomalous when `|modified_z| > 3.5`, the conventional Iglewicz-Hoaglin threshold.

One empirical finding worth calling out because it reflects the kind of validation this method requires in practice: STL's built-in `robust=True` iterative reweighting, whose stated purpose is to reduce sensitivity to outliers in the trend/seasonal fit, was measured here to *underestimate* the residual MAD on these hourly series, which inflates the modified z-score and produces a high false-positive rate. A plain (non-robust) STL fit followed by MAD-based scoring on its residual was the better-calibrated combination on this data, confirmed by comparing the false-positive rate on labeled synthetic data (`src/anomaly_detection.py`, see the comment above the `STL(...)` call). This is intentionally left visible in the code and in the tests, because "the textbook-default configuration was not the best-calibrated one, and here is the labeled evidence for the configuration that is" is a more honest and more useful engineering artifact than a detector tuned only until it looked right on one example.

### 2. Incident clustering (`src/incident_clustering.py`)

Real incidents rarely announce themselves on one metric alone. Anomalous intervals across all metrics are merged into incidents using a union-find over an "overlap within a correlation window" relation (default: 3 hours), a standard event-correlation approach that keeps the logic auditable, with no learned model or hidden state involved.

### 3. Retrieval-augmented generation over a runbook corpus (`src/retrieval.py`, `runbooks/`)

The knowledge base is six markdown "runbooks," four describing the specific failure archetypes this project's synthetic generator injects, and two deliberately confusable distractors (a network-partition doc and a clock-skew doc) so that retrieval quality is actually being tested rather than trivially guaranteed by a corpus with only "correct answers" in it.

Retrieval uses **TF-IDF with cosine similarity** by default rather than a dense embedding model. This is a deliberate, stated choice, not a cost-cutting shortcut: on a small, technical-vocabulary corpus where exact terminology (metric names, service names, failure-mode terms) matters more than paraphrase similarity, TF-IDF is competitive with dense embeddings, is fully deterministic (which the evaluation harness depends on), and has zero model-download footprint. One concrete implementation detail worth documenting because it is easy to get wrong silently: metric names use `snake_case` (`db_connection_pool_utilization_pct`), and scikit-learn's default tokenizer treats underscores as word characters, so a raw metric name tokenizes as a single opaque token that shares no vocabulary with runbook prose written as "connection pool." The retriever's text preprocessor splits on `_`, `-`, and `/` before vectorizing specifically to fix this; `tests/test_retrieval.py` pins this behavior with a regression test. `src/retrieval.py` documents exactly how to swap in a `sentence-transformers` embedding model instead, since the rest of the pipeline is retriever-agnostic.

### 4. LLM-based grounded triage (`src/llm_triage.py`, `prompts/triage_prompt.md`)

The triage layer implements one `LLMClient` interface with two backends:

- **`DeterministicStubClient`** (default): applies the same grounding rule the LLM prompt asks a real model to follow, top-retrieved-runbook wins unless retrieval confidence is too low to trust, but with a fixed rule instead of a model call. This is what makes the project runnable and testable with zero external dependencies or API keys, and it is what makes the evaluation numbers below fully reproducible rather than subject to model or sampling variance.
- **`OpenAIClient`**: the real integration path. It renders `prompts/triage_prompt.md`, calls an OpenAI-compatible chat completions endpoint with `temperature=0` and JSON-mode output, and validates the response against a fixed schema. It requires `OPENAI_API_KEY` and the `openai` package; both are optional, and the client falls back to the deterministic stub with an explicit warning if either is missing, so the project still runs for anyone without an API key.

The prompt template (`prompts/triage_prompt.md`) enforces the two properties that make an LLM usable for this kind of operational decision support:

- **Grounding**: the model is instructed to cite a specific `runbook_id` from the retrieved candidates and is explicitly told to return `"unmatched"` rather than invent a root cause not present in the provided runbooks. This is the standard RAG mitigation against hallucinated root causes.
- **Calibrated confidence reporting**: confidence is requested as a discrete `low / medium / high` label rather than a numeric probability, because LLM-reported numeric confidence scores are not calibrated probabilities, and presenting them as such to an on-call engineer making a real decision would be misleading.

Sharing one interface and one evaluation harness between a deterministic backend and a real model backend is a pattern worth calling out on its own: it is what lets a team validate the retrieval and clustering stages in CI without ever calling a paid API, while still having a clear, tested path to production model quality.

### 5. Evaluation harness (`src/evaluation.py`)

Three properties are measured independently, against ground truth from the synthetic generator, because a system like this can fail at any one of them without the others being at fault, and conflating them into one end-to-end score would hide which stage needs work:

- **Detection quality**: range-based precision and recall, following the existence-based scoring philosophy of Tatbul et al., *"Precision and Recall for Time Series"* (NeurIPS 2018). Point-wise precision/recall is a poor fit for time-series anomaly detection because it heavily penalizes a detector that correctly finds a multi-hour event with slightly different boundaries than the label; range-based scoring credits any meaningful overlap with the true window instead.
- **Retrieval quality**: recall@1 and recall@3 against a hand-authored ground-truth mapping from injected anomaly archetype to the runbook written to describe it.
- **Triage accuracy**: whether the LLM client's top-line root-cause label matches that same expected runbook id.

A representative run (`python -m src.pipeline --seed 42`) produces:

```
Detection  (range-based, Tatbul et al. 2018 style):
  precision=0.57  recall=1.00  f1=0.73  (true_events=4, predicted_intervals=7)
Retrieval  (against expected runbook per incident):
  recall@1=1.00  recall@3=1.00  (n=6)
Triage     (root-cause label matches expected runbook id):
  accuracy=1.00  (n=6)
```

Recall is perfect (every injected incident is detected), precision is honestly reported at 0.57 because the untuned threshold also produces a few single-point false-positive intervals, and retrieval/triage are perfect once an incident is detected, because the runbook corpus and the synthetic archetypes were designed together. These numbers are printed by the pipeline itself on every run and are asserted as regression thresholds in `tests/test_pipeline_end_to_end.py`, so they cannot silently drift as the code changes.

## Project structure

```
.
├── README.md
├── LICENSE
├── requirements.txt
├── prompts/
│   └── triage_prompt.md          # Versioned LLM prompt template
├── runbooks/                     # Synthetic knowledge base (6 markdown docs)
├── src/
│   ├── data_synthesis.py         # Synthetic multi-metric telemetry + labeled anomalies
│   ├── anomaly_detection.py      # STL + robust MAD z-score detector
│   ├── incident_clustering.py    # Cross-metric incident correlation
│   ├── runbook_corpus.py         # Runbook loader
│   ├── retrieval.py              # TF-IDF RAG retriever
│   ├── llm_triage.py             # LLMClient interface (stub + OpenAI backends)
│   ├── evaluation.py             # Detection / retrieval / triage scoring
│   └── pipeline.py               # End-to-end orchestration + CLI
├── examples/
│   └── run_demo.py               # Thin CLI wrapper
└── tests/
    ├── test_anomaly_detection.py
    ├── test_retrieval.py
    └── test_pipeline_end_to_end.py
```

## Installation

Requires Python 3.10+.

```bash
git clone <this-repository-url>
cd telemetry-anomaly-triage-copilot
python -m venv .venv
source .venv/bin/activate   # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running it

Run the full pipeline end to end on synthetic data with the default, offline, deterministic backend:

```bash
python -m src.pipeline --seed 42
```

Write the full structured report (incidents, triage, scores) to a JSON file:

```bash
python -m src.pipeline --seed 42 --output report.json
```

Use a real LLM backend instead of the deterministic stub (requires `pip install openai` and `OPENAI_API_KEY` set in the environment; falls back to the stub with a warning otherwise):

```bash
export OPENAI_API_KEY=sk-...
python -m src.pipeline --seed 42 --backend openai
```

Run any individual stage standalone, useful when developing or debugging one module in isolation:

```bash
python -m src.data_synthesis
python -m src.anomaly_detection
python -m src.incident_clustering
python -m src.retrieval
```

## Testing

```bash
pip install pytest
pytest tests/ -v
```

The test suite covers: the robust z-score statistic on a synthetic outlier, end-to-end detection of an injected spike against a controlled false-positive-rate bound, retrieval top-1 accuracy for every anomaly archetype (including a regression test for the snake_case tokenization fix), full-pipeline reproducibility across repeated runs with the same seed, and the "unmatched" grounding path for evidence that does not match any known runbook.

## Limitations and honest scope

This is a scoped prototype, not a production system, and it is worth being explicit about what it does not do:

- The synthetic data, while designed to be realistic in shape (daily seasonality, plausible magnitudes, four distinct failure archetypes), is not real production telemetry, and real telemetry will have noise structures, missing data, and failure modes this generator does not cover.
- The runbook corpus is small and hand-authored for evaluation clarity. A production deployment would need a much larger, continuously updated corpus, and would need to handle the case where genuinely novel incidents (not covered by any runbook) are common rather than rare, which is exactly the `"unmatched"` path this project already builds in but does not stress-test at scale.
- Precision on detection (0.57 in the reported run) reflects an intentionally untuned default threshold; a real deployment would tune the modified z-score threshold per metric against a longer labeled history, and would likely add a minimum-duration filter to suppress single-point false positives before they ever reach the clustering stage.
- The `OpenAIClient` backend is a real, functioning integration path but has not been evaluated here against the same ground truth as the deterministic stub, since doing so would require an API key and would introduce nondeterminism into what is otherwise a fully reproducible evaluation.

## Context

This project was built as an independent technical exploration by a data engineer working in cloud-based data pipeline and observability tooling. It does not reference, describe, or use any employer's internal systems, products, or data; all telemetry, service names, and runbooks in this repository are synthetic and generic to the problem domain.

## License

MIT. See `LICENSE`.
