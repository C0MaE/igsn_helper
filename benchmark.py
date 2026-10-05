"""Score pipeline output against the curated records in data/known."""

from __future__ import annotations

import argparse
import difflib
import itertools
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from documents import load_document
from validators import date_forms

MANIFEST_PATH = Path("gold/curated_manifest.json")
DOCUMENT_DIRS = [Path("data/known"), Path("data")]
OUTPUT_PATH = Path("json")
RUNS_PATH = Path("benchmark_runs")
PLACEHOLDER = "???"
MIN_MATCH_SCORE = 0.01
UNCERTAIN_MARGIN = 0.1
MAX_EXHAUSTIVE = 8
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Check:
    category: str
    field: str
    passed: Optional[bool]
    expected: Any = None
    got: Any = None
    sample: Optional[str] = None


def _norm(text: Any) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"</?su[bp]>", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _flat(text: Any) -> str:
    """Comparison key that ignores spacing and punctuation ("GEL-01 _A")."""
    return re.sub(r"[\s.,;:()\[\]{}_\-–/'’\"]", "", _norm(text))


def _numbers(text: Any) -> set[float]:
    return {float(n.replace(",", ".")) for n in NUMBER_RE.findall(_norm(text))}


def _similarity(a: Any, b: Any) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _placeholder(value: Any) -> bool:
    return PLACEHOLDER in str(value)


def _ident(value: str) -> str:
    """Bare identifier: drop resolver prefixes like https://doi.org/."""
    return _norm(re.sub(r"^https?://[^/]+/(sample/)?", "", value or ""))


_date_forms = date_forms


class Source:
    """The submitted document, for "does this value appear in it?" questions."""

    def __init__(self, text: str):
        self.flat = _flat(text)
        self.numbers = _numbers(text)

    def contains(self, value: Any) -> bool:
        key = _flat(value)
        return bool(key) and key in self.flat

    def contains_date(self, value: str) -> bool:
        parts = [p for p in value.split("/") if p]
        return bool(parts) and all(any(self.contains(f) for f in _date_forms(p)) for p in parts)


def _title(record: dict) -> str:
    return next((t.get("title", "") for t in record.get("titles") or [] if not t.get("titleType")), "")


def _code(record: dict) -> str:
    return next((t.get("title", "") for t in record.get("titles") or []
                 if t.get("titleType") == "AlternativeTitle"), "")


def _abstract(record: dict) -> str:
    return next((d.get("description", "") for d in record.get("descriptions") or []
                 if d.get("descriptionType") == "Abstract"), "")


def _family(person: dict) -> str:
    return person.get("familyName") or (person.get("name") or "").split(",")[0]


def _tokens(record: dict) -> Counter:
    split = lambda text: re.findall(r"[a-z]+\d*|\d+(?:\.\d+)?", _norm(text))
    return Counter(split(_title(record)) * 2 + split(_abstract(record)))


def _similarity_matrix(gold: list[dict], ours: list[dict]) -> list[list[float]]:
    """TF-IDF cosine over title and abstract, or sample-code equality."""
    counts = [_tokens(r) for r in gold + ours]
    df = Counter(t for c in counts for t in c)
    idf = {t: math.log(1 + len(counts) / n) for t, n in df.items()}
    vectors = [{t: n * idf[t] for t, n in c.items()} for c in counts]

    def cosine(a, b):
        dot = sum(v * b.get(t, 0.0) for t, v in a.items())
        norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
        return dot / norm if norm else 0.0

    matrix = []
    for gi, g in enumerate(gold):
        row = []
        for oi, o in enumerate(ours):
            gc, oc = _flat(_code(g)), _flat(_code(o))
            text = cosine(vectors[gi], vectors[len(gold) + oi])
            row.append(1.0 if gc and oc and gc == oc else 0.9 * text)
        matrix.append(row)
    return matrix


def match_records(gold: list[dict], ours: list[dict]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Pair curated and generated records of one document."""
    if not gold or not ours:
        return [], []
    S = _similarity_matrix(gold, ours)
    if len(gold) + len(ours) <= 2 * MAX_EXHAUSTIVE:
        if len(gold) <= len(ours):
            best = max(itertools.permutations(range(len(ours)), len(gold)),
                       key=lambda p: sum(S[g][o] for g, o in enumerate(p)))
            pairs = list(enumerate(best))
        else:
            best = max(itertools.permutations(range(len(gold)), len(ours)),
                       key=lambda p: sum(S[g][o] for o, g in enumerate(p)))
            pairs = [(g, o) for o, g in enumerate(best)]
    else:
        candidates = sorted(((S[g][o], g, o) for g in range(len(gold)) for o in range(len(ours))), reverse=True)
        pairs, used_gold, used_ours = [], set(), set()
        for _, g, o in candidates:
            if g not in used_gold and o not in used_ours:
                pairs.append((g, o))
                used_gold.add(g)
                used_ours.add(o)
    pairs = sorted((g, o) for g, o in pairs if S[g][o] >= MIN_MATCH_SCORE)

    uncertain = []
    for g, o in pairs:
        others = [S[g][x] for x in range(len(ours)) if x != o] + [S[y][o] for y in range(len(gold)) if y != g]
        if others and S[g][o] - max(others) < UNCERTAIN_MARGIN:
            uncertain.append((g, o))
    return pairs, uncertain


def _score_record(g: dict, o: dict, src: Source, draft: bool, label: str, checks: list[Check]) -> None:
    def add(category, field, passed, expected=None, got=None):
        checks.append(Check(category, field, passed, expected, got, label))

    def where(value) -> str:
        return "document" if src.contains(value) else "curator"

    code = _code(g)
    if code:
        if _placeholder(code):
            add("skipped", "sample code", None, code)
        else:
            add(where(code), "sample code", _flat(code) == _flat(_code(o)), code, _code(o))

    ours_creators = {_flat(_family(c)): c for c in o.get("creators") or []}
    gold_creators = g.get("creators") or []
    if gold_creators or not draft:
        add("document", "creator count", len(gold_creators) == len(ours_creators),
            len(gold_creators), len(ours_creators))
    for gc in gold_creators:
        who = gc.get("name", "?")
        oc = ours_creators.get(_flat(_family(gc)))
        add(where(_family(gc)), f"creator {who}: present", oc is not None, who, oc and oc.get("name"))
        given = gc.get("givenName") or ""
        if _placeholder(given):
            add("skipped", f"creator {who}: given name", None, given)
        elif given:
            add(where(given), f"creator {who}: given name",
                oc is not None and _flat(oc.get("givenName")) == _flat(given), given, oc and oc.get("givenName"))
        for ident in gc.get("nameIdentifiers") or []:
            orcid = ident.get("nameIdentifier", "").rsplit("/", 1)[-1]
            ours_ids = {i.get("nameIdentifier", "").rsplit("/", 1)[-1] for i in (oc or {}).get("nameIdentifiers") or []}
            add(where(orcid), f"creator {who}: ORCID", orcid in ours_ids, orcid, sorted(ours_ids))
        for aff in gc.get("affiliation") or []:
            name = aff.get("name", "")
            ours_affs = [a.get("name", "") for a in (oc or {}).get("affiliation") or [] if a.get("name")]
            found = any(_flat(name) in _flat(a) or _flat(a) in _flat(name) for a in ours_affs)
            add(where(name), f"creator {who}: affiliation", found, name, ours_affs)
            ror = aff.get("affiliationIdentifier")
            if ror:
                ours_rors = {a.get("affiliationIdentifier") for a in (oc or {}).get("affiliation") or []} - {None}
                add(where(ror.rsplit("/", 1)[-1]), f"creator {who}: affiliation ROR", ror in ours_rors, ror, sorted(ours_rors))

    gold_contributors = [c for c in g.get("contributors") or [] if c.get("name")]
    ours_contributors = o.get("contributors") or []
    if any(c.get("contributorType") == "DataCurator" for c in gold_contributors):
        add("policy", "data curator present", any(c.get("contributorType") == "DataCurator" for c in ours_contributors),
            "DataCurator", [c.get("name") for c in ours_contributors if c.get("contributorType") == "DataCurator"])
    gold_roles = [c for c in gold_contributors if c.get("contributorType") != "DataCurator"]
    ours_roles = [c for c in ours_contributors if c.get("contributorType") != "DataCurator"]
    if gold_roles or not draft:
        add("document", "contributor count", len(gold_roles) == len(ours_roles), len(gold_roles), len(ours_roles))
    for gc in gold_roles:
        who = gc.get("name", "?")
        match = next((c for c in ours_roles if _flat(_family(c)) == _flat(_family(gc))), None)
        add("document", f"contributor {who}: role", match is not None and match.get("contributorType") == gc.get("contributorType"),
            gc.get("contributorType"), match and match.get("contributorType"))

    ours_dates = {(d.get("date"), d.get("dateType")) for d in o.get("dates") or []}
    gold_dates = [d for d in g.get("dates") or [] if d.get("date") and not _placeholder(d.get("date"))]
    for d in g.get("dates") or []:
        value, kind = d.get("date") or "", d.get("dateType")
        if not value or _placeholder(value):
            add("skipped", f"date ({kind})", None, value)
            continue
        add("document" if src.contains_date(value) else "curator", f"date {value} ({kind})",
            (value, kind) in ours_dates, f"{value} {kind}", sorted(f"{a} {b}" for a, b in ours_dates))
    if gold_dates:
        extra = sorted(f"{a} {b}" for a, b in ours_dates - {(d["date"], d.get("dateType")) for d in gold_dates})
        add("document", "dates: no extras", not extra, [], extra)

    ours_subjects = [_flat(s.get("subject")) for s in o.get("subjects") or [] if s.get("subject")]
    for s in g.get("subjects") or []:
        value = s.get("subject", "")
        found = any(_flat(value) == x or _flat(value) in x or x in _flat(value) for x in ours_subjects)
        add(where(value), f"keyword {value}", found, value, [s.get("subject") for s in o.get("subjects") or []])
    ours_related = {_ident(r.get("relatedIdentifier", "")) for r in o.get("relatedIdentifiers") or []}
    for r in g.get("relatedIdentifiers") or []:
        value = r.get("relatedIdentifier", "")
        if not value or _placeholder(value) or value.endswith("/"):
            add("skipped", "related identifier", None, value)
            continue
        add(where(_ident(value)), f"related {value}", _ident(value) in ours_related, value, sorted(ours_related))

    gold_abstract, ours_abstract = _abstract(g), _abstract(o)
    add("writing", "title similarity", None, _title(g), round(_similarity(_title(g), _title(o)), 3))
    if gold_abstract:
        add("writing", "abstract similarity", None, gold_abstract[:80], round(_similarity(gold_abstract, ours_abstract), 3))
        ours_numbers = _numbers(ours_abstract)
        for n in sorted(_numbers(gold_abstract)):
            add("document" if n in src.numbers else "curator", f"abstract value {n:g}", n in ours_numbers, n, None)
    elif draft:
        add("skipped", "abstract", None, "")

    add("policy", "publisher", _norm((o.get("publisher") or {}).get("name")) == _norm((g.get("publisher") or {}).get("name")),
        (g.get("publisher") or {}).get("name"), (o.get("publisher") or {}).get("name"))
    for key in ("resourceTypeGeneral", "resourceType"):
        expected, got = (g.get("types") or {}).get(key), (o.get("types") or {}).get(key)
        if expected:
            add("policy", key, _norm(expected) == _norm(got), expected, got)


def summarize(checks: list[Check]) -> dict:
    by = defaultdict(list)
    for c in checks:
        by[c.category].append(c)

    def scored(category):
        items = [c for c in by[category] if c.passed is not None]
        passed = sum(c.passed for c in items)
        groups = defaultdict(lambda: [0, 0])
        for c in items:
            group = c.field.split()[0].rstrip(":").rstrip("s") if c.field.startswith("dates") else c.field.split()[0]
            groups[group][0] += c.passed
            groups[group][1] += 1
        return {"passed": passed, "total": len(items),
                "score": round(passed / len(items), 3) if items else None,
                "by_field": {k: f"{v[0]}/{v[1]}" for k, v in groups.items()}}

    def mean(field):
        values = [c.got for c in by["writing"] if c.field == field]
        return round(sum(values) / len(values), 3) if values else None

    curator = by["curator"]
    return {
        "document": scored("document"),
        "policy": scored("policy"),
        "curator": {"total": len(curator), "matched_anyway": sum(bool(c.passed) for c in curator)},
        "skipped": len(by["skipped"]),
        "writing": {"title_similarity": mean("title similarity"), "abstract_similarity": mean("abstract similarity")},
    }


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("documents", {})


def curated_records(source_name: str) -> Optional[tuple[list[dict], dict]]:
    entry = load_manifest().get(source_name)
    if not entry:
        return None
    records = json.loads(Path(entry["records"]).read_text(encoding="utf-8"))
    records = records if isinstance(records, list) else [records]
    if "select" in entry:
        records = [records[i] for i in entry["select"]]
    return records, entry


def evaluate_document(source_name: str, records: list[dict], source_text: str) -> Optional[dict]:
    """Full report for one document, or None if it has no curated records."""
    curated = curated_records(source_name)
    if curated is None:
        return None
    gold, entry = curated
    draft = bool(entry.get("draft"))
    src = Source(source_text)
    checks: list[Check] = []

    pairs, uncertain = match_records(gold, records)
    checks.append(Check("document", "sample count", len(gold) == len(records), len(gold), len(records)))
    for gi, oi in pairs:
        label = _code(gold[gi]) or f"curated #{gi + 1}"
        _score_record(gold[gi], records[oi], src, draft, label, checks)
    for gi in sorted(set(range(len(gold))) - {p[0] for p in pairs}):
        checks.append(Check("document", "sample found", False, _title(gold[gi])[:80], None, f"gold {gi + 1}"))
    for oi in sorted(set(range(len(records))) - {p[1] for p in pairs}):
        checks.append(Check("document", "sample expected", False, None, _title(records[oi])[:80], f"ours {oi + 1}"))

    return {
        "document": source_name,
        "curated": entry["records"] + (f" {entry['select']}" if "select" in entry else ""),
        "draft": draft,
        "samples": {"expected": len(gold), "found": len(records), "matched": len(pairs),
                    "pairs": [{"curated": _title(gold[g])[:70], "ours": _title(records[o])[:70],
                               "uncertain": (g, o) in uncertain} for g, o in pairs]},
        "summary": summarize(checks),
        "checks": [asdict(c) for c in checks],
    }


def _percent(part: dict) -> str:
    return f"{part['passed']}/{part['total']} ({part['score']:.0%})" if part["total"] else "-"


def print_report(report: dict, details: bool = False, max_failures: int = 12) -> None:
    s, n = report["summary"], report["samples"]
    print(f"\n{report['document']}  (vs. {report['curated']}{', draft' if report['draft'] else ''})")
    print(f"  samples:           {n['expected']} expected, {n['found']} found, {n['matched']} matched")
    unsure = [p for p in n["pairs"] if p["uncertain"]]
    if unsure:
        print(f"  [WARN] {len(unsure)} uncertain sample pairing(s), per-sample results there may compare different samples:")
        for p in unsure:
            print(f"    curated: {p['curated'][:60]}  <->  ours: {p['ours'][:60]}")
    print(f"  from the document: {_percent(s['document'])}   "
          + "  ".join(f"{k} {v}" for k, v in s["document"]["by_field"].items()))
    print(f"  policy:            {_percent(s['policy'])}")
    print(f"  curator additions: {s['curator']['total']} values not in the document (not scored), "
          f"{s['curator']['matched_anyway']} of them matched anyway (e.g. by ORCID/ROR lookups)")
    if s["skipped"]:
        print(f"  skipped:           {s['skipped']} placeholder values in the draft")
    w = s["writing"]
    print(f"  writing:           title similarity {w['title_similarity']}, abstract similarity {w['abstract_similarity']}")
    failed = [c for c in report["checks"] if c["category"] in ("document", "policy") and c["passed"] is False]
    if failed:
        shown = failed if details else failed[:max_failures]
        print(f"  failed checks ({len(failed)}):")
        for c in shown:
            where = f"[{c['sample']}] " if c["sample"] else ""
            print(f"    {where}{c['field']}: expected {c['expected']!r}, got {c['got']!r}")
        if len(shown) < len(failed):
            print(f"    ... {len(failed) - len(shown)} more, see --details")


def _find_document(name: str) -> Optional[Path]:
    return next((d / name for d in DOCUMENT_DIRS if (d / name).exists()), None)


def _output_file(document: Path) -> Path:
    from main import _safe_name
    return OUTPUT_PATH / f"{_safe_name(document.stem)}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Score outputs against the curated records in data/known")
    parser.add_argument("--run", action="store_true", help="Run the pipeline on the benchmark documents first")
    parser.add_argument("--model", help="Model for --run (default: the pipeline default)")
    parser.add_argument("--no-write", action="store_true", help="With --run: skip the writing pass")
    parser.add_argument("--no-lookup", action="store_true", help="With --run: skip ORCID/ROR lookups")
    parser.add_argument("--details", action="store_true", help="List every failed check")
    args = parser.parse_args()

    if not load_manifest():
        raise SystemExit(f"No benchmark defined: create {MANIFEST_PATH} (see gold/curated_manifest.example.json)")

    documents = []
    for name in load_manifest():
        path = _find_document(name)
        if path is None:
            print(f"[WARN] {name}: document not found in {', '.join(map(str, DOCUMENT_DIRS))}")
        else:
            documents.append(path)

    if args.run:
        import main as pipeline
        pipeline.main(model=args.model, files=[str(p) for p in documents], write=not args.no_write,
                      lookups=not args.no_lookup)

    reports = []
    for path in documents:
        output = _output_file(path)
        if not output.exists():
            print(f"\n{path.name}\n  not run yet ({output} missing), use --run")
            continue
        records = json.loads(output.read_text(encoding="utf-8"))
        records = records if isinstance(records, list) else [records]
        report = evaluate_document(path.name, records, load_document(path).text)
        provenance = output.parent / f"{output.stem}.provenance.json"
        if provenance.exists():
            prov = json.loads(provenance.read_text(encoding="utf-8"))
            report["run"] = {"generatedAt": prov.get("generatedAt"),
                             "model": (prov.get("passes") or [{}])[0].get("model"),
                             "writingPass": any(p.get("pass", "").startswith("write") for p in prov.get("passes") or [])}
            print(f"\n(output from {report['run']['generatedAt']}, model {report['run']['model']}, "
                  f"writing pass {'on' if report['run']['writingPass'] else 'off'})", end="")
        print_report(report, details=args.details)
        reports.append(report)

    if reports:
        passed = sum(r["summary"]["document"]["passed"] for r in reports)
        total = sum(r["summary"]["document"]["total"] for r in reports)
        print(f"\nOverall, values from the documents: {passed}/{total} ({passed / total:.0%}) over {len(reports)} document(s)")
        RUNS_PATH.mkdir(exist_ok=True)
        run_file = RUNS_PATH / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
        run_file.write_text(json.dumps({"reports": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report saved to {run_file}")


if __name__ == "__main__":
    main()
