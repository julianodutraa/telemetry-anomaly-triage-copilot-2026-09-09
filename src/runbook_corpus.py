"""Loader for the synthetic runbook corpus used as the RAG knowledge base.

Runbooks live as plain markdown files under `runbooks/` with a small,
hand-rollable frontmatter block (id, title, tags) so the corpus can be
inspected and extended by a human without touching any code. No YAML
dependency is introduced for what is a three-field, fixed-shape header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
DEFAULT_RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "runbooks"


@dataclass
class Runbook:
    id: str
    title: str
    tags: list[str]
    body: str
    path: Path

    @property
    def searchable_text(self) -> str:
        """Text used for retrieval: title and tags are repeated to up-weight
        them relative to prose in a plain TF-IDF vector space model."""
        return f"{self.title} {self.title} {' '.join(self.tags)} {' '.join(self.tags)} {self.body}"


def _parse_frontmatter(raw: str) -> dict:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        raise ValueError("Runbook file is missing a --- frontmatter block")
    header, body = match.group(1), match.group(2)
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return {"header": fields, "body": body.strip()}


def _parse_tags(raw_tags: str) -> list[str]:
    raw_tags = raw_tags.strip()
    if raw_tags.startswith("[") and raw_tags.endswith("]"):
        raw_tags = raw_tags[1:-1]
    return [t.strip() for t in raw_tags.split(",") if t.strip()]


def load_runbook(path: Path) -> Runbook:
    raw = path.read_text(encoding="utf-8")
    parsed = _parse_frontmatter(raw)
    header = parsed["header"]
    return Runbook(
        id=header["id"],
        title=header["title"],
        tags=_parse_tags(header.get("tags", "")),
        body=parsed["body"],
        path=path,
    )


def load_corpus(directory: Path | str = DEFAULT_RUNBOOK_DIR) -> list[Runbook]:
    directory = Path(directory)
    paths = sorted(directory.glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"No runbook markdown files found under {directory}")
    return [load_runbook(p) for p in paths]


if __name__ == "__main__":
    for rb in load_corpus():
        print(f"{rb.id}: {rb.title}  [{', '.join(rb.tags)}]")
