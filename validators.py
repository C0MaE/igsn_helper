"""Deterministic checks that need no model.

The most damaging failure mode for an IGSN record is a plausible-looking but
invented identifier: a wrong ORCID silently attributes a sample to a different
researcher. Every identifier in the output must appear verbatim in the source
document, and that is a string comparison, not a judgement call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+")
ROR_RE = re.compile(r"ror\.org/(0[a-hj-km-np-tv-z0-9]{6}\d{2})", re.IGNORECASE)

SEVERITY_ORDER = {"error": 0, "warning": 1}


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    kind: str      # "orcid" | "doi" | "ror" | "creator-name"
    path: str      # JSON path where the value was found
    value: str
    message: str

    def as_dict(self) -> dict:
        return {
            "severity": self.severity,
            "kind": self.kind,
            "path": self.path,
            "value": self.value,
            "message": self.message,
        }


def _normalize(text: str) -> str:
    """Collapse whitespace and case so docx extraction artefacts don't matter.

    Extracted .docx text often contains stray spaces inside runs, e.g.
    "https://orcid.org/0000- 0003-2361-6775".
    """
    return re.sub(r"\s+", "", text).lower()


def _walk(obj: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _walk(value, f"{path}[{i}]")
    else:
        yield path, obj


def check_identifiers(igsn_data: dict, source_text: str) -> list[Finding]:
    """Flag every identifier in the record that is absent from the source."""
    haystack = _normalize(source_text)
    findings: list[Finding] = []

    patterns = [
        ("orcid", ORCID_RE),
        ("doi", DOI_RE),
        ("ror", ROR_RE),
    ]

    for path, value in _walk(igsn_data):
        if not isinstance(value, str) or not value:
            continue
        # The publisher is a fixed field set in code, so its ROR is expected
        # to be absent from the questionnaire.
        if path.startswith("publisher."):
            continue
        for kind, pattern in patterns:
            for match in pattern.finditer(value):
                # ROR_RE captures the bare id; the others match in full.
                found = match.group(1) if pattern is ROR_RE else match.group(0)
                if _normalize(found) not in haystack:
                    findings.append(
                        Finding(
                            severity="error",
                            kind=kind,
                            path=path,
                            value=found,
                            message=f"{kind.upper()} does not appear in the source document",
                        )
                    )
    return findings


def check_creator_names(igsn_data: dict, source_text: str) -> list[Finding]:
    """Flag creators and contributors whose family name is not in the source."""
    haystack = _normalize(source_text)
    findings: list[Finding] = []

    for role in ("creators", "contributors"):
        for i, person in enumerate(igsn_data.get(role) or []):
            if not isinstance(person, dict):
                continue
            # Fall back to the part before the comma of "LastName, FirstName".
            family = person.get("familyName") or person.get("name", "").split(",")[0]
            family = (family or "").strip()
            if not family:
                continue
            if _normalize(family) not in haystack:
                findings.append(
                    Finding(
                        severity="error",
                        kind="creator-name",
                        path=f"{role}[{i}].familyName",
                        value=family,
                        message="Family name does not appear in the source document",
                    )
                )
    return findings


def check_publisher_untouched(igsn_data: dict) -> list[Finding]:
    """The publisher and schemaVersion are fixed fields, not extracted ones."""
    findings: list[Finding] = []

    expected_schema = "http://datacite.org/schema/kernel-4"
    if igsn_data.get("schemaVersion") != expected_schema:
        findings.append(
            Finding(
                severity="warning",
                kind="fixed-field",
                path="schemaVersion",
                value=str(igsn_data.get("schemaVersion")),
                message=f"Expected {expected_schema}",
            )
        )

    publisher = igsn_data.get("publisher") or {}
    if publisher.get("name") != "Kiel University":
        findings.append(
            Finding(
                severity="warning",
                kind="fixed-field",
                path="publisher.name",
                value=str(publisher.get("name")),
                message="Expected 'Kiel University'",
            )
        )
    return findings


NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def date_forms(iso: str) -> list[str]:
    """Ways an ISO date may be written in a document (2026-06-25, 25.06.2026, ...)."""
    forms = [iso]
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if m:
        y, mo, d = map(int, m.groups())
        name = MONTH_NAMES[mo - 1]
        forms += [f"{d:02d}.{mo:02d}.{y}", f"{d}.{mo}.{y}", f"{d} {name} {y}",
                  f"{name} {d}, {y}", f"{d}. {name} {y}", f"{d:02d}/{mo:02d}/{y}"]
    return forms


def check_dates_in_source(igsn_data: dict, source_text: str) -> list[Finding]:
    """A date that appears nowhere in the document was made up."""
    haystack = _normalize(source_text)
    findings = []
    for i, d in enumerate(igsn_data.get("dates") or []):
        value = str(d.get("date") or "")
        parts = [p for p in value.split("/") if p]
        if parts and not all(any(_normalize(f) in haystack for f in date_forms(p)) for p in parts):
            findings.append(Finding(
                severity="warning",
                kind="date",
                path=f"dates[{i}].date",
                value=value,
                message="date not found in the source document, please check",
            ))
    return findings


def _numbers(text: str) -> set[str]:
    text = re.sub(r"</?su[bp]>", "", text)
    return {n.replace(",", ".") for n in NUMBER_RE.findall(text)}


def check_numbers(igsn_data: dict, source_text: str) -> list[Finding]:
    """Numbers in the title and abstract must come from the document.

    Title and abstract are written by the model, and a plausible but wrong
    mass or percentage is the error a reader is least likely to notice.
    Values a curator computes (e.g. 200 mg - 45.0 mg) are flagged too, which
    is intended: they need a second look.
    """
    allowed = _numbers(source_text)
    texts = [("titles[0].title", ((igsn_data.get("titles") or [{}])[0].get("title") or ""))]
    texts += [(f"descriptions[{i}].description", d.get("description") or "")
              for i, d in enumerate(igsn_data.get("descriptions") or [])]
    findings = []
    for path, text in texts:
        new = sorted(_numbers(text) - allowed)
        if new:
            findings.append(Finding(
                severity="warning",
                kind="number",
                path=path,
                value=", ".join(new),
                message="number(s) not found in the source document, please check",
            ))
    return findings


def check_created_dates(igsn_data: dict) -> list[Finding]:
    """More than one Created date is a judgement call for a human."""
    created = [d.get("date") for d in igsn_data.get("dates") or [] if d.get("dateType") == "Created"]
    if len(created) > 1:
        return [Finding(
            severity="warning",
            kind="dates",
            path="dates",
            value=", ".join(str(c) for c in created),
            message="more than one Created date, please check which event created the sample",
        )]
    return []


def check_samples(records: list[dict]) -> list[Finding]:
    """Document-level checks across all samples of one document."""
    findings: list[Finding] = []
    seen: dict[str, int] = {}
    for i, record in enumerate(records):
        title = ((record.get("titles") or [{}])[0].get("title") or "").strip().lower()
        if title in seen:
            findings.append(Finding(
                severity="warning",
                kind="duplicate-title",
                path=f"records[{i}].titles[0]",
                value=title,
                message=f"same title as sample {seen[title] + 1}, samples must be distinguishable",
            ))
        else:
            seen[title] = i
    return findings


def _without_curators(igsn_data: dict) -> dict:
    """The DataCurator comes from config, not from the document, so it is
    excluded from the checks that compare the record with the source."""
    contributors = [c for c in igsn_data.get("contributors") or []
                    if c.get("contributorType") != "DataCurator"]
    return {**igsn_data, "contributors": contributors}


def check_missing_identifiers(igsn_data: dict, source_text: str) -> list[Finding]:
    """The other direction: ORCIDs and DOIs in the document that the record lacks."""
    record_text = _normalize(" ".join(str(v) for _, v in _walk(igsn_data)))
    findings = []
    for kind, pattern in (("orcid", ORCID_RE), ("doi", DOI_RE)):
        for found in sorted({m.group(0).rstrip(".,;:)") for m in pattern.finditer(source_text)}):
            if _normalize(found) not in record_text:
                findings.append(Finding(
                    severity="warning", kind=f"missing-{kind}", path="", value=found,
                    message=f"{kind.upper()} in the document but not in the record, was it missed?",
                ))
    return findings


def validate(igsn_data: dict, source_text: str) -> list[Finding]:
    """Run every deterministic check, most severe first."""
    extracted = _without_curators(igsn_data)
    findings = (
        check_identifiers(extracted, source_text)
        + check_creator_names(extracted, source_text)
        + check_publisher_untouched(igsn_data)
        + check_created_dates(igsn_data)
        + check_dates_in_source(igsn_data, source_text)
        + check_missing_identifiers(igsn_data, source_text)
        + check_numbers(igsn_data, source_text)
    )
    return sorted(findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))


def print_findings(findings: list[Finding]) -> None:
    if not findings:
        print("  No hallucinated identifiers or names found")
        return

    errors = sum(1 for f in findings if f.severity == "error")
    warnings = len(findings) - errors
    print(f"  {errors} error(s), {warnings} warning(s):")
    for f in findings:
        icon = "[ERROR]" if f.severity == "error" else "[WARN]"
        print(f"    {icon} {f.path}: {f.value} — {f.message}")
