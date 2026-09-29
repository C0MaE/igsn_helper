"""Complete records with ORCID and ROR lookups, as curators do by hand.

    uv run python enrich.py json/<document>.json [...]   # enrich finished outputs afterwards
    uv run python enrich.py --offline json/...           # only use cached lookups

- ORCID: every ORCID taken from the document is looked up in the public
  ORCID registry. The registered name replaces the document's ("Rossi, A."
  -> "Rossi, Anna"). If the family names disagree, the registry name
  is used as well but flagged for review: the document may have swapped
  given and family name ("K. Ahmed" registered as "Hassan, Karim Ahmed").
- ROR: every affiliation without an identifier is sent to ROR's affiliation
  matching. Only a match ROR marks as chosen (unambiguous) is used; it sets
  the organization's English name and ROR ID, as in the curated records.

No account is needed for either API. Answers are cached in .lookup_cache,
so reruns and tests work offline. Without network the pipeline runs as
before and says that lookups were skipped; this script can then enrich the
outputs later on a machine with internet access.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from validators import ORCID_RE, Finding

CACHE_PATH = Path("./.lookup_cache")
TIMEOUT_S = 15
ORCID_URL = "https://pub.orcid.org/v3.0/{orcid}/person"
ROR_URL = "https://api.ror.org/v2/organizations?affiliation={query}"
USER_AGENT = "igsn_helper (Kiel University IGSN metadata tool)"


def _plain(text: str) -> str:
    """Case- and accent-insensitive comparison key ("Müller" == "Muller")."""
    text = unicodedata.normalize("NFKD", text or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in text if not unicodedata.combining(c)).lower())


@dataclass
class Lookup:
    """Cached HTTP lookups; after the first network failure it stops trying."""

    offline: bool = False
    cache_path: Path = CACHE_PATH
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    cache_hits: int = 0

    def _get(self, url: str) -> Optional[dict]:
        self.cache_path.mkdir(exist_ok=True)
        cache_file = self.cache_path / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.json"
        if cache_file.exists():
            self.cache_hits += 1
            return json.loads(cache_file.read_text(encoding="utf-8"))
        if self.offline:
            return None
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                data = json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                data = {}  # e.g. an ORCID that does not exist: a valid, cacheable answer
            else:
                self.errors.append(f"{url}: HTTP {e.code}")
                return None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # No network (e.g. the GPU server): skip the remaining lookups too.
            self.errors.append(f"{url}: {e}")
            self.offline = True
            return None
        self.requests += 1
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def orcid_name(self, orcid: str) -> Optional[tuple[str, str]]:
        """(given, family) as registered, or None."""
        data = self._get(ORCID_URL.format(orcid=orcid))
        name = (data or {}).get("name") or {}
        given = ((name.get("given-names") or {}).get("value") or "").strip()
        family = ((name.get("family-name") or {}).get("value") or "").strip()
        return (given, family) if given and family else None

    def ror(self, affiliation: str) -> Optional[tuple[str, str]]:
        """(ROR ID URL, English name) of an unambiguous match, or None.

        Curated records use English names. Some organizations have only a
        native-language label in ROR (many German universities) and their
        English forms as aliases; then the English alias
        closest to the document's wording is used.
        """
        data = self._get(ROR_URL.format(query=urllib.parse.quote(affiliation)))
        chosen = next((item for item in (data or {}).get("items", []) if item.get("chosen")), None)
        if not chosen:
            return None
        org = chosen["organization"]
        names = org.get("names") or []
        english = next((n["value"] for n in names if "label" in n.get("types", []) and n.get("lang") == "en"), None)
        display = next((n for n in names if "ror_display" in n.get("types", [])), None)
        if english:
            return org["id"], english
        if display and display.get("lang") in ("en", None):
            return org["id"], display["value"]
        # Aliases can be former names (an institute renamed years ago keeps
        # its old English name as alias), so an alias only wins if it is
        # closer to how the document names the organization.
        candidates = ([display["value"]] if display else []) + [
            n["value"] for n in names if "alias" in n.get("types", []) and n.get("lang") == "en"]
        if not candidates:
            return org["id"], affiliation
        ratio = lambda a: difflib.SequenceMatcher(None, a.lower(), affiliation.lower()).ratio()
        return org["id"], max(candidates, key=ratio)


def _cased(registered: str, document: str) -> str:
    """Registry names are sometimes all caps ("ROSSI"). Keep the document's
    spelling when it is the same name, else fix all-caps or all-lowercase."""
    if document and _plain(document) == _plain(registered) and not document.isupper():
        return document
    if registered.isupper() or registered.islower():
        return re.sub(r"[^\W\d_]+", lambda m: m.group(0).capitalize(), registered.lower())
    return registered


def _enrich_person(person: dict, lookup: Lookup, changes: list[str], findings: list[Finding], path: str) -> None:
    for ident in person.get("nameIdentifiers") or []:
        match = ORCID_RE.search(ident.get("nameIdentifier", ""))
        if not match:
            continue
        registered = lookup.orcid_name(match.group(0))
        if not registered:
            continue
        given, family = registered
        family = _cased(family, person.get("familyName") or "")
        given = _cased(given, person.get("givenName") or "")
        before = person.get("name")
        new_name = f"{family}, {given}"
        if new_name != before:
            person.update({"name": new_name, "givenName": given, "familyName": family})
            changes.append(f"{before} -> {new_name} (ORCID record)")
            if _plain(family) != _plain((before or "").split(",")[0]):
                findings.append(Finding(
                    severity="warning", kind="orcid-name", path=f"{path}.name", value=f"{before} -> {new_name}",
                    message="family name in the document differs from the ORCID record, please check",
                ))
        break


def _enrich_affiliations(person: dict, lookup: Lookup, changes: list[str], unmatched: set[str]) -> None:
    for aff in person.get("affiliation") or []:
        text = aff.get("name") or ""
        if not text or aff.get("affiliationIdentifier"):
            continue
        match = lookup.ror(text)
        if not match:
            if not lookup.offline:
                unmatched.add(text)
            continue
        ror_id, name = match
        aff.update({"name": name, "affiliationIdentifier": ror_id,
                    "affiliationIdentifierScheme": "ROR", "schemeURI": "https://ror.org/"})
        changes.append(f"affiliation {text!r} -> {name} ({ror_id})")


def enrich_records(records: list[dict], lookup: Lookup) -> tuple[list[dict], list[str], list[Finding]]:
    """Returns (records, changes, findings). Records are changed in place."""
    changes: list[str] = []
    findings: list[Finding] = []
    unmatched: set[str] = set()
    for i, record in enumerate(records):
        for role in ("creators", "contributors"):
            for j, person in enumerate(record.get(role) or []):
                if person.get("contributorType") == "DataCurator":
                    continue  # comes from .env, not from the document
                _enrich_person(person, lookup, changes, findings, f"records[{i}].{role}[{j}]")
                _enrich_affiliations(person, lookup, changes, unmatched)
    changes = list(dict.fromkeys(changes))  # the same person appears in every sample
    changes += [f"no unambiguous ROR match for affiliation {text!r}" for text in sorted(unmatched)]
    if lookup.errors:
        changes.append(f"lookups skipped, no connection ({len(lookup.errors)} failed): {lookup.errors[0]}")
    return records, changes, findings


def status(lookup: Lookup) -> dict:
    return {"requests": lookup.requests, "cacheHits": lookup.cache_hits,
            "offline": lookup.offline, "errors": lookup.errors[:5]}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Enrich finished outputs with ORCID and ROR lookups")
    parser.add_argument("files", nargs="+", help="Output JSON files (json/<document>.json)")
    parser.add_argument("--offline", action="store_true", help="Only use cached lookups")
    args = parser.parse_args()

    lookup = Lookup(offline=args.offline)
    for name in args.files:
        path = Path(name)
        records = json.loads(path.read_text(encoding="utf-8"))
        records = records if isinstance(records, list) else [records]
        records, changes, findings = enrich_records(records, lookup)
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{path.name}: {len(changes)} change(s), {len(findings)} warning(s)")
        for c in changes:
            print(f"  └─ {c}")
        for f in findings:
            print(f"  [WARN] {f.value}: {f.message}")

        sidecar = path.parent / f"{path.stem}.provenance.json"
        if sidecar.exists():
            prov = json.loads(sidecar.read_text(encoding="utf-8"))
            prov.setdefault("postprocessing", []).extend(changes)
            prov["enrichment"] = status(lookup)
            validation = prov.setdefault("validation", {"findings": [], "errorCount": 0})
            validation["findings"].extend(f.as_dict() for f in findings)
            sidecar.write_text(json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")
    if lookup.errors:
        print(f"[WARN] {len(lookup.errors)} lookup(s) failed, e.g. {lookup.errors[0]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
