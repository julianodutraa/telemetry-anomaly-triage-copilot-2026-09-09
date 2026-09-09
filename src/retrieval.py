"""TF-IDF retrieval over the runbook corpus (the "R" in this RAG pipeline).

A dense sentence-transformer embedding model would work here too, and
the `RunbookRetriever` interface is written so that swapping the
vectorizer for one is a localized change (see the docstring at the
bottom of this file). TF-IDF is used as the shipped default because it
has zero model-download footprint, is fully deterministic, and is
genuinely competitive with dense embeddings on this kind of short,
technical-vocabulary corpus where exact terminology (service names,
metric names, error terms) matters more than paraphrase similarity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from src.runbook_corpus import Runbook, load_corpus

_WORD_JOINER_RE = re.compile(r"[_\-/]+")


def _normalize(text: str) -> str:
    """Split snake_case metric names and hyphenated identifiers into
    separate words before vectorization.

    Scikit-learn's default TF-IDF token pattern treats underscores as
    word characters, so a raw metric name like
    `db_connection_pool_utilization_pct` is tokenized as a single opaque
    token that shares no vocabulary at all with the runbook prose talking
    about "connection pool utilization". Splitting on `_`, `-`, and `/`
    first is what lets identifier-style evidence text actually match
    natural-language runbook content.
    """
    return _WORD_JOINER_RE.sub(" ", text)


@dataclass
class RetrievedRunbook:
    runbook: Runbook
    score: float


class RunbookRetriever:
    def __init__(self, runbooks: list[Runbook] | None = None):
        self.runbooks = runbooks or load_corpus()
        self.vectorizer = TfidfVectorizer(
            lowercase=True,
            ngram_range=(1, 2),
            stop_words="english",
            min_df=1,
            preprocessor=_normalize,
        )
        corpus_texts = [rb.searchable_text for rb in self.runbooks]
        self._matrix = self.vectorizer.fit_transform(corpus_texts)

    def retrieve(self, query: str, top_k: int = 3) -> list[RetrievedRunbook]:
        query_vec = self.vectorizer.transform([query])
        sims = cosine_similarity(query_vec, self._matrix).ravel()
        order = np.argsort(-sims)[:top_k]
        return [RetrievedRunbook(runbook=self.runbooks[i], score=float(sims[i])) for i in order]


if __name__ == "__main__":
    retriever = RunbookRetriever()
    demo_query = (
        "db_connection_pool_utilization_pct on service 'primary-db' deviated abnormally "
        "for about 4h, gradually climbing and plateauing near its ceiling"
    )
    for hit in retriever.retrieve(demo_query, top_k=3):
        print(f"{hit.score:.3f}  {hit.runbook.id}  ({hit.runbook.title})")

# --------------------------------------------------------------------------
# Upgrading to dense embeddings
# --------------------------------------------------------------------------
# To swap in a sentence-transformer embedding model instead of TF-IDF,
# replace the vectorizer with something implementing `.encode(list[str])
# -> np.ndarray` (e.g. `sentence_transformers.SentenceTransformer`), embed
# `corpus_texts` once at construction time, embed the query in `retrieve`,
# and rank by cosine similarity exactly as above. The rest of the pipeline
# (incident clustering, triage, evaluation) is retriever-agnostic and does
# not need to change.
