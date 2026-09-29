"""Local settings from a .env file next to this module.

Values are only used where the environment does not already set them, so a
variable exported in the shell wins over the file. Any variable can live in
.env, including OLLAMA_HOST for the GPU server; see .env.example.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

ENV_PATH = Path(__file__).parent / ".env"
ORCID_RE = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")


def load_env(path: Path = ENV_PATH) -> None:
    """Minimal KEY=VALUE parser: comments, blank lines and quotes are handled."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value.startswith("#"):  # KEY=   # comment  -> empty value
            value = ""
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key.strip(), value)


def data_curator() -> Optional[dict]:
    """The DataCurator contributor added to every record, from IGSN_CURATOR_*.

    Returns None if IGSN_CURATOR_NAME is not set. Raises ValueError for a
    malformed name or ORCID, so a typo fails at start-up instead of ending
    up in every record.
    """
    name = os.environ.get("IGSN_CURATOR_NAME", "").strip()
    if not name:
        return None
    family, comma, given = (part.strip() for part in name.partition(","))
    if not comma or not family or not given:
        raise ValueError(f'IGSN_CURATOR_NAME must be "FamilyName, GivenName", got {name!r}')

    curator = {
        "name": f"{family}, {given}",
        "nameType": "Personal",
        "givenName": given,
        "familyName": family,
        "contributorType": "DataCurator",
        "nameIdentifiers": [],
        "affiliation": [],
    }

    orcid = os.environ.get("IGSN_CURATOR_ORCID", "").strip()
    if orcid:
        match = ORCID_RE.search(orcid)
        if not match:
            raise ValueError(f"IGSN_CURATOR_ORCID is not an ORCID iD: {orcid!r}")
        curator["nameIdentifiers"].append({
            "nameIdentifier": f"https://orcid.org/{match.group(0)}",
            "nameIdentifierScheme": "ORCID",
            "schemeURI": "https://orcid.org/",
        })

    affiliation = os.environ.get("IGSN_CURATOR_AFFILIATION", "").strip()
    if affiliation:
        entry = {"name": affiliation}
        ror = os.environ.get("IGSN_CURATOR_AFFILIATION_ROR", "").strip()
        if ror:
            ror_id = ror.rstrip("/").rsplit("/", 1)[-1]
            entry |= {
                "affiliationIdentifier": f"https://ror.org/{ror_id}",
                "affiliationIdentifierScheme": "ROR",
                "schemeURI": "https://ror.org/",
            }
        curator["affiliation"].append(entry)
    return curator


load_env()
