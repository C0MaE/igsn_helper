"""Provenance records for generated IGSN metadata.

Written as a sidecar file next to each record rather than inside it: the JSON
in ./json has to stay a valid DataCite kernel-4 document, and an extra
top-level key would break schema validation on submission.

The point is to be able to answer, a year from now, "which model produced this
field, from which document, with which prompt" without guessing.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from llm_client import LLMResponse, sha256_text

PROVENANCE_SUFFIX = ".provenance.json"


def _git_revision() -> Optional[str]:
    """The commit the pipeline ran at, so the prompts are recoverable too."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _git_dirty() -> Optional[bool]:
    """True if tracked or new files differ from gitRevision.

    Without this flag a record produced by uncommitted code would point to a
    commit whose prompts and rules it was never generated with.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal", "--", "*.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class ProvenanceRecorder:
    """Collects one entry per pipeline pass for a single source document."""

    def __init__(self, source_file: Path, source_text: str):
        self.source_file = source_file
        self.source_text = source_text
        self.started_at = datetime.now(timezone.utc)
        self.passes: list[dict] = []
        self.findings: list[dict] = []
        self.changes: list[str] = []
        self.evaluation: Optional[dict] = None
        self.loading: dict = {}
        self.enrichment: Optional[dict] = None

    def record_pass(self, name: str, response: LLMResponse, prompt: str) -> None:
        entry = {"pass": name, "promptChars": len(prompt)}
        entry.update(response.provenance())
        self.passes.append(entry)

    def record_findings(self, findings: list) -> None:
        self.findings.extend(f.as_dict() for f in findings)

    def record_loading(self, document) -> None:
        """Format and loader warnings of the source document."""
        self.loading = document.provenance()

    def record_changes(self, changes: list[str]) -> None:
        """Where postprocessing overrode or dropped model output."""
        self.changes.extend(changes)

    def record_evaluation(self, summary: dict) -> None:
        self.evaluation = summary

    def build(self) -> dict:
        return {
            "generatedBy": "igsn_helper",
            "gitRevision": _git_revision(),
            "gitDirty": _git_dirty(),
            "generatedAt": self.started_at.isoformat(),
            "source": {
                "filename": self.source_file.name,
                "sha256": sha256_text(self.source_text),
                "chars": len(self.source_text),
                **self.loading,
            },
            "passes": self.passes,
            "postprocessing": self.changes,
            "enrichment": self.enrichment,
            "evaluation": self.evaluation,
            "validation": {
                "findings": self.findings,
                "errorCount": sum(1 for f in self.findings if f["severity"] == "error"),
            },
        }

    def write(self, json_output_file: Path) -> Path:
        """Write the sidecar next to the record it describes."""
        target = json_output_file.with_suffix("")
        target = target.parent / f"{target.name}{PROVENANCE_SUFFIX}"
        target.write_text(
            json.dumps(self.build(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target
