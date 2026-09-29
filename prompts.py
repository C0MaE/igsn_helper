"""Extraction prompt.

The JSON structure is enforced by constrained decoding against
igsn.IGSNExtraction, so the prompt no longer spells out a schema. It carries
only what the schema cannot express: what counts as a sample, where each
field comes from, and how to decide between vocabulary values.

Submissions are free text from many institutions without a template, so the
prompt describes what to look for, not where it sits in a particular form.

Deliberately contains no example values taken from real documents: small
models copy them into other documents. An earlier prompt showed the Kiel
University ROR, which ended up in every affiliation; a later one showed a
role and a mass from one real submission, and every author of an unrelated
document got that role, and its sample that mass. Examples stay generic,
and postprocess.verify_extraction drops
copied values that do not occur in the document.
"""

import json
from pathlib import Path

IGSN_EXTRACTION_PROMPT = """\
You extract metadata for registering physical research samples (IGSN) from a
document a researcher sent in. Use only information written in the document.
COPY, DO NOT WRITE: every text field is copied from the document as written;
titles and abstracts are written in a later step, not here.
Never invent names, identifiers, dates, affiliations or numbers. Instruction
texts of a form (e.g. "This should include ...") are not content; ignore them.

SAMPLES
A sample is one physical object that gets its own identifier: e.g. a pellet,
a powder mixture, a specimen, a core section, a bioprinted construct.
A document may describe one sample or many, e.g. one per table row or one per
section "sample 1", "sample 2", ... List every sample exactly once, in the
order of the document. Do not split one sample into several, and do not merge
different samples.

shared — information that applies to all samples of the document:
  preparation — the text describing how the samples were prepared or
    processed, if it applies to all samples; copied as written. Empty if none.
  creators — every person named as involved, in the order given.
    - name: "FamilyName, GivenName" as written, e.g. "Rossi, A." or
      "Rossi, Anna". Convert "Anna Rossi" or "A. Rossi" to this form.
    - givenName / familyName: the two parts of that name.
    - role: the role or task the document states for this person, copied as
      written. Empty if the document states none; never infer a role.
    - nameIdentifiers: the ORCID given for that person, if any, with
      nameIdentifierScheme "ORCID" and schemeURI "https://orcid.org/".
      A person without an ORCID in the document gets an empty list.
    - affiliation: the institution(s) given for that person. Markers such
      as <sup>1</sup> after a name refer to a numbered list of institutions.
      Copy the institution text as written.
  subjects — keywords that apply to all samples.
  dates — dated events that apply to all samples (see dates below).
  relatedIdentifiers — references that apply to all samples (see below).

samples — one entry per sample:
  sampleCode — the name or code the document uses for this sample, exactly
    as written (a sample ID, or a label like "sample 3"). Not an identifier
    that several samples share, such as an experiment, proposal, beamtime
    or project number. Empty if there is none.
  givenTitle — the title the document gives this sample, copied as written.
    Empty if there is none.
  givenDescription — the description or abstract the document gives this
    sample, copied as written. Empty if there is none.
  facts — short facts about this sample only, each copied from the
    document: composition, formula, mass, amounts, treatment, remarks.
    For a table row, one fact per column: "<column name>: <cell value>".
  subjects — keywords specific to this sample, e.g. its main compound.
  dates / relatedIdentifiers — only those specific to this sample.

dates — one entry per event for which the document gives a date; date is
  that date as written, dateInformation describes the event. No entry for
  events without a date.
  - "Created": the step in which the sample itself came into existence
    (e.g. bioprinted, synthesized, pressed, mixed, cast). Exactly one event
    per sample; preparing its ingredients beforehand does not count.
  - "Collected": a natural sample was taken from the field or from a patient.
  - "Other": every other event, e.g. culturing, treatment, fixation,
    embedding, sectioning, measurement.

relatedIdentifiers — identifiers of related works (DOIs etc.).
  - relatedIdentifierType "DOI" for DOIs.
  - relationType "References" when the work provides background or methods;
    "IsDocumentedBy" when the work describes these samples themselves.
  - resourceTypeGeneral: "JournalArticle" for journal papers, "Dataset" for
    data, "Text" for other documents.

Document:
{text}
"""


SAMPLE_WRITING_PROMPT = """\
You write the title and abstract of one physical sample for its IGSN
registration, in the style of the repository examples below.

title — names what the sample physically is and what distinguishes it from
  the other samples of the same submission (compound, composition,
  treatment).
abstract — one to four full sentences built from all the information below
  (given description, facts and preparation): what the sample is, what it
  is made of and how it was prepared, with every value specific to this
  sample (composition, masses, purities, treatment, dimensions).
keywords — one to three keywords for this sample only, e.g. the name of its
  main compound or material as written in the information below. Not
  keywords that apply to every sample of the submission.
issues — contradictions or doubts in the information below that a curator
  should check, e.g. values that do not fit the stated composition, or text
  that seems to belong to a different sample. Empty list if none.

Rules:
- Use only the information below. Do not add facts.
- Do not calculate new values: every number you write must appear in the
  information below.
- If the given title already is a title that names this specific sample,
  keep its wording and only fix formatting. Turn a sentence into a title,
  and replace text that does not name this sample (e.g. "All samples are
  powders").
- Keep the wording of the given description where it fits, but complete it
  with the facts and the preparation.

{examples}
The sample:
Sample code: {code}
Title given in the document: {given_title}
Description given in the document: {given_description}
Facts:
{facts}
Preparation (applies to all samples of the submission): {preparation}
Other samples in the same submission: {others}
"""

STYLE_EXAMPLES_PATH = Path(__file__).parent / "style_examples.json"
MAX_STYLE_EXAMPLES = 4


def _style_examples(source_document: str) -> str:
    """Curated examples, never those taken from the document being processed."""
    try:
        examples = json.loads(STYLE_EXAMPLES_PATH.read_text(encoding="utf-8"))["examples"]
    except (OSError, ValueError, KeyError):
        return ""
    usable = [e for e in examples if source_document not in e.get("source_documents", [])]
    if not usable:
        return ""
    blocks = [f"Title: {e['title']}\nAbstract: {e['abstract']}" for e in usable[:MAX_STYLE_EXAMPLES]]
    return "Repository examples (other submissions, for style only):\n\n" + "\n\n".join(blocks) + "\n"


def get_sample_writing_prompt(extraction: dict, index: int, source_document: str) -> str:
    """Prompt for pass 2: title and abstract of samples[index]."""
    shared = extraction.get("shared") or {}
    samples = extraction.get("samples") or []
    sample = samples[index]
    others = [
        o.get("sampleCode") or o.get("givenTitle") or (o.get("facts") or [""])[0]
        for i, o in enumerate(samples) if i != index
    ]
    return SAMPLE_WRITING_PROMPT.format(
        examples=_style_examples(source_document),
        code=sample.get("sampleCode") or "(none)",
        given_title=sample.get("givenTitle") or "(none)",
        given_description=sample.get("givenDescription") or "(none)",
        facts="\n".join(f"- {f}" for f in sample.get("facts") or []) or "(none)",
        preparation=shared.get("preparation") or "(none)",
        others=", ".join(o[:60] for o in others if o) or "(none)",
    )


# num_ctx is 16384 tokens. ~30k characters of English are ~8-9k tokens, which
# leaves room for these instructions and the JSON output.
MAX_DOCUMENT_CHARS = 30000


def get_igsn_extraction_prompt(file_data: str, max_chars: int = MAX_DOCUMENT_CHARS) -> str:
    """Generate IGSN extraction prompt with file data."""
    if len(file_data) > max_chars:
        print(f"  [WARN] Document truncated from {len(file_data)} to {max_chars} characters")
    return IGSN_EXTRACTION_PROMPT.format(text=file_data[:max_chars])
