# igsn-helper

Draft [DataCite](https://schema.datacite.org/)/[IGSN](https://www.igsn.org/) metadata records for physical samples from the free-text submissions researchers send in (DOCX or PDF, no template), using a local language model via [Ollama](https://ollama.com/).

The output is a draft for a curator, not a finished registration: every record comes with a provenance file that states where each value came from, what the code changed, and what needs a second look.

## How it works

```
document ──► load ──► pass 1: copy facts ──► verify copies ──► pass 2: write title/abstract ──► build records ──► validate ──► ORCID/ROR lookup ──► JSON + provenance + PDF
 (docx/pdf)                (one call)          (no model)         (one call per sample, optional)   (policy fields)   (no model)     (optional)
```

1. **Load** (`documents.py`, `pdf_loader.py`): text in reading order, including tables (as Markdown), form fields, text boxes, footnotes, headers, list numbering, link targets and super-/subscripts. Two-column PDF text is read column by column. Content that gets lost while loading (e.g. an ORCID) or scanned pages without a text layer are reported.
2. **Pass 1, extract** (`prompts.py`): the model *copies* people, roles, identifiers, dates, keywords and per-sample facts. One document may describe many samples; each becomes its own record. Output is constrained to a JSON schema (`igsn.py`).
3. **Verify copies** (`postprocess.verify_extraction`): values that do not occur in the document are dropped, roles must stand next to the person's name, and ORCIDs the model missed are recovered.
4. **Pass 2, write** (optional): a short call per sample writes title, abstract and keywords from the verified facts, in the style of curated examples.
5. **Build records** (`postprocess.py`): fixed repository fields, role mapping to DataCite `contributorType`, ISO dates, plain-text formulas (`Co₃O₄`), the data curator from `.env`.
6. **Validate** (`validators.py`): identifiers, names and dates must appear in the document, numbers in written text must come from the document, missed ORCIDs/DOIs, duplicate titles, several `Created` dates.
7. **Enrich** (`enrich.py`, optional): full names from the ORCID registry (flagging names that contradict it) and ROR IDs for affiliations. Works offline from cache; can also be applied later.

## Requirements

- Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/) with a model. Developed with `qwen3:8b` on a 16 GB Apple M1 Pro; the default is `qwen3:14b`. The sampling settings in `llm_client.py` follow Qwen3's recommendations and should be revisited for other models.
- Internet access only for ORCID/ROR lookups (optional).

## Setup

```bash
uv sync
cp .env.example .env        # enter the data curator added to every record
ollama pull qwen3:8b
```

## Usage

Put submissions into `data/`, start Ollama (`ollama serve`), then:

```bash
uv run python main.py --model qwen3:8b                  # every document in data/
uv run python main.py --model qwen3:8b path/to/file.pdf  # selected documents
```

| Option | Effect |
|---|---|
| `--model` | Ollama model tag |
| `--host` | Ollama server, e.g. a GPU machine through an SSH tunnel (or `OLLAMA_HOST` in `.env`) |
| `--no-write` | skip pass 2; titles and abstracts come from the document text |
| `--no-lookup` | skip ORCID/ROR lookups |
| `--no-cache` | ignore cached model answers |
| `--max-tokens` | limit per model answer (default 8192) |

Model answers are cached by content hash in `.llm_cache/`, so changes to rules and checks can be re-run on existing results in seconds. A looping model is stopped early; its partial output is kept in `.llm_debug/`.

Other tools:

```bash
uv run python documents.py data/          # preview what the model will see, without calling it
uv run python enrich.py json/<doc>.json   # add ORCID/ROR data to finished outputs afterwards
uv run python benchmark.py [--run]        # score outputs against curated records (see below)
```

## Output

For every document `data/<name>`:

- `json/<name>.json`: a list of DataCite records, one per sample
- `json/<name>.provenance.json`: source file and hash, model and options, prompt hashes, every change made by the code, lookup status, validation findings and benchmark score
- `pdf/<name>__<nn>_<title>.pdf`: one summary per sample

## Configuration

`.env` (template: `.env.example`):

| Variable | Meaning |
|---|---|
| `IGSN_CURATOR_NAME` | `"FamilyName, GivenName"` of the data curator added to every record |
| `IGSN_CURATOR_ORCID` | the curator's ORCID iD |
| `IGSN_CURATOR_AFFILIATION`, `IGSN_CURATOR_AFFILIATION_ROR` | the curator's affiliation |
| `OLLAMA_HOST` | Ollama server URL |

Repository conventions are fixed in `postprocess.py`: publisher (Kiel University), `resourceTypeGeneral` `PhysicalObject`, `resourceType` `Material sample`, publication year (registration year unless the document states one) and the mapping of stated roles to DataCite contributor types (`ROLE_MAP`). Adjust them there for another repository.

Style examples for pass 2 are read from `style_examples.json` (template: `style_examples.example.json`). Without it, pass 2 runs without examples.

## Benchmark

`benchmark.py` compares outputs with records a curator made from the same submissions. Put submissions and curated record files into `data/known/` and map them in `gold/curated_manifest.json` (template: `gold/curated_manifest.example.json`).

Every expected value is classified automatically:

- **document**: it appears in the submission, so extraction should find it; this is the score
- **curator**: it does not (looked-up names, computed masses, dates sent by e-mail); counted separately, not as an error
- **policy**: fixed repository fields
- **skipped**: placeholders in draft records

Titles and abstracts are compared by similarity. Samples are paired by sample code, otherwise by distinctive words; uncertain pairings are flagged. Each run is saved to `benchmark_runs/`.

## Data protection

Submissions contain unpublished sample descriptions and personal data. `data/`, `json/`, `pdf/`, `gold/`, `benchmark_runs/`, the caches, `style_examples.json` and `.env` are excluded from version control by `.gitignore`. Keep it that way.

## Limitations

- Titles and abstracts are drafts; their style differs from curated text, and values a curator would compute (e.g. binder masses) are not calculated but flagged for review.
- Scanned PDFs need OCR first; XFA forms may lose field values (both are reported).
- Accuracy has been measured on a small set of curated submissions; more curated examples make the benchmark more reliable.

## Project layout

| File | Purpose |
|---|---|
| `main.py` | pipeline entry point |
| `documents.py`, `pdf_loader.py` | loading DOCX/PDF, preview |
| `prompts.py` | prompts for both passes |
| `igsn.py` | DataCite models and extraction schemas |
| `llm_client.py` | Ollama client: cache, streaming, loop detection |
| `postprocess.py` | copy verification, record building, repository policy |
| `validators.py` | deterministic checks |
| `enrich.py` | ORCID/ROR lookups |
| `provenance.py` | provenance sidecar files |
| `benchmark.py` | scoring against curated records |
| `pdf_generator.py` | PDF summaries |
| `config.py` | `.env` loading, data curator |
