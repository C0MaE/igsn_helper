"""Turn the model's extraction into a complete IGSN record."""

from __future__ import annotations

import copy
import re
from datetime import date
from typing import Optional

from igsn import IGSN
from validators import ORCID_RE, _normalize

PUBLISHER = {
    "name": "Kiel University",
    "publisherIdentifier": "https://ror.org/04v76ef78",
    "publisherIdentifierScheme": "ROR",
    "schemeURI": "https://ror.org/",
}
SCHEMA_VERSION = "http://datacite.org/schema/kernel-4"
# Repository conventions; adjust for another repository.
RESOURCE_TYPE_GENERAL = "PhysicalObject"
RESOURCE_TYPE = "Material sample"
DEFAULT_LANGUAGE = "en"

ADDITIONAL_INFO_MARKER = "Additional information"
PUBLICATION_YEAR_RE = re.compile(r"publi\w*[^.\n]{0,80}?\b((?:19|20)\d{2})\b", re.IGNORECASE)

DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def _publication_year(source_text: str, today: date) -> tuple[str, str]:
    """Year stated in the additional-information section, else the registration year."""
    _, marker, additional = source_text.partition(ADDITIONAL_INFO_MARKER)
    if marker:
        match = PUBLICATION_YEAR_RE.search(additional)
        if match:
            return match.group(1), f"publicationYear: {match.group(1)} (stated in source)"
    return str(today.year), f"publicationYear: {today.year} (registration year, none stated in source)"


def _verified_orcids(creator: dict, haystack: str, changes: list[str]) -> list[dict]:
    """Keep only ORCIDs that appear verbatim in the source, in canonical form."""
    kept = []
    for ident in creator.get("nameIdentifiers") or []:
        match = ORCID_RE.search(ident.get("nameIdentifier", ""))
        if not match:
            changes.append(f"dropped malformed identifier for {creator.get('name')}: {ident.get('nameIdentifier')!r}")
            continue
        orcid = match.group(0)
        if _normalize(orcid) not in haystack:
            changes.append(f"dropped ORCID not found in source for {creator.get('name')}: {orcid}")
            continue
        kept.append({
            "nameIdentifier": f"https://orcid.org/{orcid}",
            "nameIdentifierScheme": "ORCID",
            "schemeURI": "https://orcid.org/",
        })
    return kept


def _normalize_related(related: list[dict], changes: list[str]) -> list[dict]:
    """DataCite expects bare DOIs ("10.xxxx/..."), not resolver URLs."""
    out = []
    for item in related:
        item = dict(item)
        if item.get("relatedIdentifierType") == "DOI":
            bare = DOI_PREFIX_RE.sub("", item.get("relatedIdentifier", "")).strip()
            if bare != item.get("relatedIdentifier"):
                changes.append(f"normalized DOI to {bare}")
            item["relatedIdentifier"] = bare
        out.append(item)
    return out


def _unique(items: list[dict], key) -> list[dict]:
    """Drop repeats, e.g. a keyword listed both for all samples and for one."""
    seen, out = set(), []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


ACADEMIC_TITLE_RE = re.compile(
    r"\b(?:prof|dr|pd|phd|ph\.d|dipl|ing|rer|nat|med|phil|habil|m\.sc|b\.sc|msc|bsc)\.?(?:-\w+\.?)?(?=\s|$)",
    re.IGNORECASE,
)


def _strip_titles(value: Optional[str]) -> Optional[str]:
    """"Dr. Sebastian" -> "Sebastian"; DataCite names carry no academic titles."""
    if not value:
        return value
    return re.sub(r"\s+", " ", ACADEMIC_TITLE_RE.sub("", value)).strip(" ,")


def _creator(c: dict, haystack: str, changes: list[str]) -> dict:
    given, family = _strip_titles(c.get("givenName")), _strip_titles(c.get("familyName"))
    name = f"{family}, {given}" if family and given else _strip_titles(c.get("name"))
    return {
        "name": name,
        "nameType": "Personal",
        "givenName": given,
        "familyName": family,
        "nameIdentifiers": _verified_orcids(c, haystack, changes),
        "affiliation": [{"name": a["name"]} for a in c.get("affiliation") or [] if a.get("name")],
    }


SUBSCRIPTS = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
SUPERSCRIPTS = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")
SCRIPT_RE = re.compile(r"<(sub|sup)>(.*?)</\1>", re.DOTALL)


def _plain(value):
    """Turn the loader's <sub>/<sup> markup into plain Unicode text."""
    if isinstance(value, str):
        def convert(match):
            table = SUBSCRIPTS if match.group(1) == "sub" else SUPERSCRIPTS
            inner = match.group(2)
            converted = inner.translate(table)
            return converted if all(c != o or not c.isalnum() for c, o in zip(converted, inner)) else inner
        return SCRIPT_RE.sub(convert, value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


# Document roles -> DataCite contributorType, as the curators map them. First match wins.
ROLE_MAP = [
    (r"data\s*curat", "DataCurator"),
    (r"data\s*manag", "DataManager"),
    (r"data\s*collect|measur|operator", "DataCollector"),
    (r"produc|prepar|synthe|fabricat", "Producer"),
    (r"project\s*lead", "ProjectLeader"),
    (r"project\s*manag", "ProjectManager"),
    (r"project\s*member", "ProjectMember"),
    (r"work\s*package", "WorkPackageLeader"),
    (r"lead|supervis|\bpi\b|principal|head|owner|contact|correspond|responsible", "ContactPerson"),
    (r"editor", "Editor"),
    (r"sponsor|fund", "Sponsor"),
    (r"rights?\s*holder", "RightsHolder"),
    (r"research|scientist|technician|student|postdoc", "Researcher"),
]


def contributor_type(role: str) -> Optional[str]:
    role = role.strip().lower()
    for pattern, contributor_type in ROLE_MAP:
        if re.search(pattern, role):
            return contributor_type
    return None


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS |= {"mär": 3, "mai": 5, "okt": 10, "dez": 12}


def _iso_date(value: str) -> Optional[str]:
    """Unambiguous date notations to ISO 8601; None if not safely convertible."""
    v = value.strip()
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?(/\d{4}(-\d{2}(-\d{2})?)?)?", v):
        return v
    m = re.fullmatch(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", v)
    if m:
        d, mo, y = map(int, m.groups())
    else:
        m = (re.fullmatch(r"(\d{1,2})\.?\s+([A-Za-zä]+)\.?\s+(\d{4})", v)
             or re.fullmatch(r"([A-Za-zä]+)\.?\s+(\d{1,2}),?\s+(\d{4})", v))
        if not m:
            return None
        a, b, y = m.groups()
        day, name = (a, b) if a.isdigit() else (b, a)
        mo = MONTHS.get(name[:3].lower())
        if mo is None:
            return None
        d, y = int(day), int(y)
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _dates(items: list[dict], changes: list[str]) -> list[dict]:
    out = []
    for item in items:
        item = dict(item)
        raw = str(item.get("date") or "")
        if not re.search(r"\d{4}", raw):
            changes.append(f"dropped {item.get('dateType') or 'date'} entry without a date: {raw!r}")
            continue
        iso = _iso_date(raw)
        if iso is None:
            changes.append(f"date {raw!r} is not ISO 8601 and was not converted, please fix by hand")
        elif iso != raw:
            changes.append(f"converted date {raw!r} to {iso}")
            item["date"] = iso
        out.append(item)
    return out


def _fallback_text(sample: dict, shared: dict) -> tuple[str, str]:
    """Title and abstract assembled from copied text, without the writing pass."""
    facts = [f.strip().rstrip(".") for f in sample.get("facts") or [] if f.strip()]
    title = ((sample.get("givenTitle") or "").strip()
             or (re.sub(r"^[^:]{1,40}:\s*", "", facts[0]) if facts else "")
             or (sample.get("sampleCode") or "").strip())
    abstract = (sample.get("givenDescription") or "").strip()
    if not abstract:
        parts = (["; ".join(facts) + "."] if facts else []) + [(shared.get("preparation") or "").strip()]
        abstract = " ".join(p for p in parts if p)
    return title, abstract


def _titles(sample: dict, title: str, number: int, haystack: str, changes: list[str]) -> list[dict]:
    """The title, plus the document's own sample code as AlternativeTitle."""
    title = (title or "").strip()
    titles = [{"title": title, "lang": "en-US"}] if title else []
    code = (sample.get("sampleCode") or "").strip()
    if code and _normalize(code) != _normalize(title):
        if _normalize(code) in haystack:
            titles.append({"title": code, "titleType": "AlternativeTitle", "lang": "en-US"})
        else:
            changes.append(f"sample {number}: dropped sample code not found in source: {code!r}")
    return titles


SHINGLE = 4
MIN_COPIED_RATIO = 0.8


def _plain_source(source_text: str) -> str:
    return _normalize(re.sub(r"</?su[bp]>", "", source_text))


def _copied_ratio(text: str, source_text: str) -> float:
    """How much of a longer text occurs in the document, by 4-word shingles."""
    words = lambda t: re.findall(r"\w+", re.sub(r"</?su[bp]>", "", t).lower())
    ours, theirs = words(text), words(source_text)
    grams = lambda w: {tuple(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}
    mine = grams(ours)
    if not mine:
        return 1.0 if _normalize(text) in _normalize(source_text) else 0.0
    return len(mine & grams(theirs)) / len(mine)


ROLE_WINDOW = 80


def _tokens_in_source(text: str, source_text: str) -> bool:
    """Every number and every content word of text occurs in the document."""
    tokenize = lambda t: {w.strip(".").lower() for w in re.findall(r"[\w.]+", re.sub(r"</?su[bp]>", "", t))}
    source = tokenize(source_text)
    needed = {w for w in tokenize(text) if any(ch.isdigit() for ch in w) or len(w) >= 4}
    return needed <= source


def _role_near_name(role: str, family_name: str, source_text: str) -> bool:
    """A role counts only where it is written next to the person's name."""
    text = re.sub(r"\s+", " ", re.sub(r"</?su[bp]>", "", source_text)).lower()
    role, name = role.strip().lower(), family_name.strip().lower()
    if not role or not name:
        return False
    for m in re.finditer(re.escape(name), text):
        window = text[max(0, m.start() - ROLE_WINDOW):m.end() + ROLE_WINDOW]
        if role in window:
            return True
    return False


def _is_copied(text: str, source_text: str, haystack: str) -> bool:
    key = _normalize(re.sub(r"</?su[bp]>", "", text or ""))
    if not key:
        return True
    return key in haystack or _copied_ratio(text, source_text) >= MIN_COPIED_RATIO


def _recover_orcids(people: list[dict], source_text: str, changes: list[str]) -> None:
    """Give a person the ORCID written right after their name if the model missed it."""
    text = re.sub(r"\s+", " ", re.sub(r"</?su[bp]>", "", source_text))
    lower = text.lower()
    families = [(p.get("familyName") or (p.get("name") or "").split(",")[0]).strip() for p in people]
    used = {m.group(0) for p in people for i in p.get("nameIdentifiers") or []
            for m in [ORCID_RE.search(i.get("nameIdentifier", ""))] if m}
    for person, family in zip(people, families):
        if person.get("nameIdentifiers") or not family:
            continue
        for m in re.finditer(re.escape(family.lower()), lower):
            end = min([lower.find(f.lower(), m.end()) for f in families if f and f != family and lower.find(f.lower(), m.end()) != -1]
                      + [m.end() + ROLE_WINDOW])
            found = ORCID_RE.search(text[m.end():end])
            if found and found.group(0) not in used:
                person["nameIdentifiers"] = [{"nameIdentifier": f"https://orcid.org/{found.group(0)}",
                                              "nameIdentifierScheme": "ORCID", "schemeURI": "https://orcid.org/"}]
                used.add(found.group(0))
                changes.append(f"added ORCID {found.group(0)} for {person.get('name')}: written next to the name, missed by the model")
                break


TITLE_CODE_RE = re.compile(r"\(([^()]{2,40})\)\s*\.?\s*$")


def _code_from_title(sample: dict, where: str, changes: list[str]) -> None:
    """Prefer the code a title ends with over one taken from elsewhere."""
    title, code = sample.get("givenTitle") or "", (sample.get("sampleCode") or "").strip()
    if code and (len(code.split()) > 3 or len(code) > 40):
        changes.append(f"{where}: dropped sample code {code[:50]!r}...: a title, not a code")
        sample["sampleCode"] = code = ""
    match = TITLE_CODE_RE.search(title)
    if not match:
        return
    candidate = match.group(1).strip()
    looks_like_code = len(candidate.split()) <= 2 and re.search(r"[\d_]|[A-Z]{2,}", candidate)
    if looks_like_code and _normalize(candidate) != _normalize(code) and (not code or _normalize(code) not in _normalize(title)):
        sample["sampleCode"] = candidate
        changes.append(f"{where}: sample code {code or '(none)'!r} replaced by {candidate!r} from the end of its title")


def verify_extraction(extraction: dict, source_text: str) -> tuple[dict, list[str]]:
    """Drop pass-1 values that are not copied from the document."""
    extraction = copy.deepcopy(extraction)
    haystack = _plain_source(source_text)
    changes: list[str] = []
    shared = extraction.get("shared") or {}

    _recover_orcids(shared.get("creators") or [], source_text, changes)

    for person in shared.get("creators") or []:
        role = (person.get("role") or "").strip()
        family = person.get("familyName") or (person.get("name") or "").split(",")[0]
        if role and not _role_near_name(role, family, source_text):
            changes.append(f"dropped role {role!r} of {person.get('name')}: not next to the name in the document")
            person["role"] = ""

    preparation = shared.get("preparation") or ""
    if preparation and not _is_copied(preparation, source_text, haystack):
        changes.append("dropped the shared preparation text: not copied from the document")
        shared["preparation"] = ""

    def clean_subjects(subjects: list, where: str) -> list:
        kept = []
        for subject in subjects or []:
            value = (subject.get("subject") or "").strip()
            if not value:
                continue
            if not (_is_copied(value, source_text, haystack) or _tokens_in_source(value, source_text)):
                changes.append(f"{where}: dropped keyword {value!r}: not in the document")
                continue
            kept.append({"subject": value})
        return kept

    shared["subjects"] = clean_subjects(shared.get("subjects"), "shared")

    for number, sample in enumerate(extraction.get("samples") or [], 1):
        where = f"sample {number}"
        for field in ("givenTitle", "givenDescription"):
            value = sample.get(field) or ""
            if value and not _is_copied(value, source_text, haystack):
                changes.append(f"{where}: dropped {field}, not copied from the document")
                sample[field] = ""
        facts = []
        for fact in sample.get("facts") or []:
            value = fact.split(":", 1)[1] if ":" in fact else fact
            if _is_copied(value, source_text, haystack) or _tokens_in_source(value, source_text):
                facts.append(fact)
            else:
                changes.append(f"{where}: dropped fact {fact!r}: not in the document")
        sample["facts"] = facts
        sample["subjects"] = clean_subjects(sample.get("subjects"), where)
        _code_from_title(sample, where, changes)
    return extraction, changes


def build_records(
    extraction: dict,
    source_text: str,
    today: Optional[date] = None,
    texts: Optional[list[Optional[dict]]] = None,
    curator: Optional[dict] = None,
) -> tuple[list[dict], list[str]]:
    """One complete IGSN record per sample. Returns (records, changes)."""
    today = today or date.today()
    haystack = _normalize(source_text)
    changes: list[str] = []

    shared = extraction.get("shared") or {}
    extracted_people = shared.get("creators") or []
    creators = [_creator(c, haystack, changes) for c in extracted_people]

    contributors = []
    for person, creator in zip(extracted_people, creators):
        role = (person.get("role") or "").strip()
        if not role:
            continue
        mapped = contributor_type(role)
        if mapped is None:
            mapped = "Other"
            changes.append(f"role {role!r} of {creator['name']} has no mapping, used 'Other', please check")
        contributors.append({**copy.deepcopy(creator), "contributorType": mapped})
    if curator:
        contributors.append(copy.deepcopy(curator))
    shared_related = _normalize_related(shared.get("relatedIdentifiers") or [], changes)

    publication_year, year_note = _publication_year(source_text, today)
    changes.append(year_note)
    if curator is None:
        changes.append("no data curator configured (IGSN_CURATOR_NAME in .env), records have no contributors")

    samples = extraction.get("samples") or []
    if not samples:
        changes.append("the model found no samples in the document")

    records = []
    for number, sample in enumerate(samples, 1):
        text = texts[number - 1] if texts and number <= len(texts) else None
        keywords = []
        for keyword in (text or {}).get("keywords") or []:
            keyword = (keyword or "").strip()
            if keyword and _normalize(keyword) == _normalize(sample.get("sampleCode") or ""):
                continue  # a sample code is not a keyword
            if keyword and (_normalize(keyword) in haystack or _tokens_in_source(keyword, source_text)):
                keywords.append({"subject": keyword})
            elif keyword:
                changes.append(f"sample {number}: dropped written keyword {keyword!r}: not in the document")
        title, abstract = _fallback_text(sample, shared)
        if text and (text.get("title") or "").strip() and (text.get("abstract") or "").strip():
            title, abstract = text["title"].strip(), text["abstract"].strip()
        else:
            changes.append(f"sample {number}: title and abstract taken from the document text (no writing pass)")
        record = {
            "doi": "",
            "url": "",
            "creators": copy.deepcopy(creators),
            "titles": _titles(sample, title, number, haystack, changes),
            "publisher": dict(PUBLISHER),
            "publicationYear": publication_year,
            "types": {
                "resourceTypeGeneral": RESOURCE_TYPE_GENERAL,
                "resourceType": RESOURCE_TYPE,
            },
            "schemaVersion": SCHEMA_VERSION,
            "contributors": copy.deepcopy(contributors),
            "dates": sorted(
                _unique(_dates((shared.get("dates") or []) + (sample.get("dates") or []), changes),
                        key=lambda d: (d.get("date"), d.get("dateType"))),
                key=lambda d: d.get("date", ""),
            ),
            "language": DEFAULT_LANGUAGE,
            "descriptions": [{"description": abstract, "descriptionType": "Abstract", "lang": "en-US"}]
                            if abstract else [],
            "subjects": _unique((shared.get("subjects") or []) + (sample.get("subjects") or []) + keywords,
                                key=lambda x: (x.get("subject") or "").strip().lower()),
            "relatedIdentifiers": _unique(
                shared_related + _normalize_related(sample.get("relatedIdentifiers") or [], changes),
                key=lambda r: (r.get("relatedIdentifier") or "").lower()),
        }

        validated = IGSN.model_validate(_plain(record)).model_dump(mode="json", exclude_none=True)

        if not validated.get("contributors"):
            validated.pop("contributors", None)
        records.append(validated)

    return records, list(dict.fromkeys(changes))
