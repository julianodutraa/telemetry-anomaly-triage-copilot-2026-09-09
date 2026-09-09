"""LLM-based root-cause triage, grounded in retrieved runbooks.

Two backends implement the same `LLMClient` interface:

- `DeterministicStubClient` is the default. It applies the exact same
  grounding rule a well-prompted LLM is asked to follow (top-retrieved-
  runbook wins, unless retrieval confidence is too low to trust), but
  does so with a fixed rule instead of a model call. This keeps the
  end-to-end example fully offline, free, and byte-for-byte
  reproducible, which is what makes automated testing and the
  evaluation harness in `evaluation.py` meaningful: the "LLM" step
  cannot introduce nondeterminism into a CI run.
- `OpenAIClient` is the real integration path: it renders
  `prompts/triage_prompt.md`, calls an OpenAI-compatible chat completions
  endpoint, and validates the response against the structured schema.
  It requires `OPENAI_API_KEY` and the `openai` package; both are
  optional, and the pipeline falls back to the stub with a warning if
  either is missing, so the project still runs for anyone without an
  API key.

This split mirrors a pattern that shows up in serious production RAG
systems: a deterministic "golden" backend for tests and CI, and a real
model backend for production, sharing one interface and one evaluation
harness so the two are directly comparable.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from src.incident_clustering import Incident
from src.retrieval import RetrievedRunbook

PROMPT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "triage_prompt.md"
LOW_CONFIDENCE_SCORE_THRESHOLD = 0.05


@dataclass
class TriageReport:
    incident_id: int
    root_cause_label: str
    confidence: str
    explanation: str
    recommended_action: str
    cited_runbook_ids: list[str]
    backend: str

    def as_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "root_cause_label": self.root_cause_label,
            "confidence": self.confidence,
            "explanation": self.explanation,
            "recommended_action": self.recommended_action,
            "cited_runbook_ids": self.cited_runbook_ids,
            "backend": self.backend,
        }


class LLMClient(ABC):
    name: str = "abstract"

    @abstractmethod
    def triage(self, incident: Incident, retrieved: list[RetrievedRunbook]) -> TriageReport:
        raise NotImplementedError


_NUMBERED_STEP_RE = re.compile(r"^\d+\.\s*(.+)$")


def _first_recommended_step(body: str) -> str:
    """Pull the first numbered step out of a runbook's "Recommended triage
    steps" section, so the stub's recommendation is grounded in the
    runbook text rather than templated filler.

    Steps are markdown paragraphs that soft-wrap across multiple physical
    lines, so each step's continuation lines are joined until the next
    numbered item or a section break, rather than reading only the first
    physical line.
    """
    marker = "## Recommended triage steps"
    if marker not in body:
        return "Review the cited runbook in full before acting."
    section = body.split(marker, 1)[1]

    lines = section.splitlines()
    step_lines: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        if _NUMBERED_STEP_RE.match(stripped):
            if collecting:
                break  # reached the second numbered step; stop
            collecting = True
            step_lines.append(_NUMBERED_STEP_RE.match(stripped).group(1))
        elif collecting:
            if not stripped or stripped.startswith("#"):
                break
            step_lines.append(stripped)
    if not step_lines:
        return "Review the cited runbook in full before acting."
    return " ".join(step_lines).strip()


class DeterministicStubClient(LLMClient):
    """Offline, reproducible triage backend used as the default and in tests."""

    name = "deterministic-stub-v1"

    def triage(self, incident: Incident, retrieved: list[RetrievedRunbook]) -> TriageReport:
        if not retrieved or retrieved[0].score < LOW_CONFIDENCE_SCORE_THRESHOLD:
            return TriageReport(
                incident_id=incident.incident_id,
                root_cause_label="unmatched",
                confidence="low",
                explanation=(
                    "No retrieved runbook scored highly enough against the incident "
                    "evidence to ground a hypothesis; this incident likely represents "
                    "a failure mode not yet covered by the runbook corpus."
                ),
                recommended_action="Escalate to a human for manual investigation and, "
                                    "if confirmed novel, author a new runbook.",
                cited_runbook_ids=[],
                backend=self.name,
            )

        top = retrieved[0]
        second_score = retrieved[1].score if len(retrieved) > 1 else 0.0
        margin = top.score - second_score
        confidence = "high" if margin > 0.05 and top.score > 0.15 else "medium"

        return TriageReport(
            incident_id=incident.incident_id,
            root_cause_label=top.runbook.id,
            confidence=confidence,
            explanation=(
                f"Incident evidence ({incident.evidence_summary()}) most closely matches "
                f"'{top.runbook.title}' (retrieval score {top.score:.3f})."
            ),
            recommended_action=_first_recommended_step(top.runbook.body),
            cited_runbook_ids=[r.runbook.id for r in retrieved if r.score > LOW_CONFIDENCE_SCORE_THRESHOLD],
            backend=self.name,
        )


def _render_prompt(incident: Incident, retrieved: list[RetrievedRunbook]) -> str:
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    retrieved_block = "\n".join(
        f"- id: {r.runbook.id} (score {r.score:.3f})\n  title: {r.runbook.title}\n  body: {r.runbook.body[:600]}..."
        for r in retrieved
    )
    return (
        template
        .replace("{{evidence_summary}}", incident.evidence_summary())
        .replace("{{retrieved_runbooks}}", retrieved_block)
    )


class OpenAIClient(LLMClient):
    """Real LLM backend. Requires `openai` and `OPENAI_API_KEY`.

    Falls back to `DeterministicStubClient` with a warning if either is
    unavailable, so callers can request this backend unconditionally and
    still get a usable (if less capable) result in environments without
    API access, such as this project's default CI/demo run.
    """

    name = "openai-gpt-json-v1"

    def __init__(self, model: str = "gpt-4o-mini"):
        self.model = model
        self._fallback = DeterministicStubClient()
        self._client = None
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            warnings.warn(
                "OPENAI_API_KEY not set; OpenAIClient will fall back to the "
                "deterministic stub backend for every call.",
                stacklevel=2,
            )
            return
        try:
            import openai  # noqa: F401 (imported lazily; optional dependency)
            self._client = openai.OpenAI(api_key=api_key)
        except ImportError:
            warnings.warn(
                "The 'openai' package is not installed; OpenAIClient will fall "
                "back to the deterministic stub backend for every call.",
                stacklevel=2,
            )

    def triage(self, incident: Incident, retrieved: list[RetrievedRunbook]) -> TriageReport:
        if self._client is None:
            report = self._fallback.triage(incident, retrieved)
            report.backend = f"{self.name}(fallback={report.backend})"
            return report

        prompt = _render_prompt(incident, retrieved)
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        payload = json.loads(response.choices[0].message.content)
        return TriageReport(
            incident_id=incident.incident_id,
            root_cause_label=payload["root_cause_label"],
            confidence=payload["confidence"],
            explanation=payload["explanation"],
            recommended_action=payload["recommended_action"],
            cited_runbook_ids=payload.get("cited_runbook_ids", []),
            backend=self.name,
        )


def get_default_client(backend: str = "stub") -> LLMClient:
    if backend == "stub":
        return DeterministicStubClient()
    if backend == "openai":
        return OpenAIClient()
    raise ValueError(f"Unknown backend: {backend}")
