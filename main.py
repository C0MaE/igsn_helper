import json
import time
from pathlib import Path
from documents import LoadedDocument, is_input_file, load_document
from prompts import MAX_DOCUMENT_CHARS, get_igsn_extraction_prompt, get_sample_writing_prompt
from pdf_generator import generate_igsn_pdf
from igsn import IGSNExtraction, SampleText
from llm_client import OllamaClient
from postprocess import build_records, verify_extraction
import config
from provenance import ProvenanceRecorder
import benchmark
import enrich
import validators

PATH = Path("./data")
PATH.mkdir(exist_ok=True)
JSON_OUTPUT_PATH = Path("./json")
JSON_OUTPUT_PATH.mkdir(exist_ok=True)

# The model fills only the content fields; fixed fields and policies are
# added by postprocess.build_record.
EXTRACTION_SCHEMA = IGSNExtraction.model_json_schema()
SAMPLE_TEXT_SCHEMA = SampleText.model_json_schema()


def format_time(seconds):
    if seconds < 60:
        return f"{seconds:.2f}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        return f"{int(minutes)}m {secs:.0f}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{int(hours)}h {int(minutes)}m"

def get_all_unused_files():
    return sorted(f for f in PATH.iterdir() if is_input_file(f))

def load_documents(files: list[Path]) -> list[LoadedDocument]:
    """Load every file; one broken file must not stop the batch."""
    documents = []
    for file in files:
        try:
            doc = load_document(file)
        except Exception as e:
            print(f"  [ERROR] {file.name}: could not be read ({e})")
            continue
        icon = "[SKIP]" if doc.fatal else ("[WARN]" if doc.warnings else "[OK]")
        print(f"  {icon} {file.name} ({doc.format}, {len(doc.text)} chars)")
        for warning in doc.warnings:
            print(f"      └─ {warning}")
        if doc.fatal:
            print("      └─ skipped")
            continue
        if len(doc.text) > MAX_DOCUMENT_CHARS:
            doc.warnings.append(
                f"document has {len(doc.text)} chars, only the first {MAX_DOCUMENT_CHARS} are sent to the model"
            )
            print(f"      └─ [WARN] {doc.warnings[-1]}")
        documents.append(doc)
    return documents

def extract_data_to_json(client: OllamaClient, file_data: str):
    """Run the extraction pass. Returns (extraction, response, prompt)."""
    print(f"Starting data extraction with Ollama ({client.model})...")
    prompt = get_igsn_extraction_prompt(file_data)
    print(f"  └─ Prompt length: {len(prompt)} characters")

    # Thinking off: the schema constrains the output anyway, and reasoning
    # tokens only cost time here.
    response = client.chat(prompt, schema=EXTRACTION_SCHEMA, think=False)

    if response.cached:
        print("  └─ Cache hit")
    else:
        print(f"Response from Ollama received ({format_time(response.duration_s)})")

    # Constrained decoding guarantees parseable JSON, so no fence stripping.
    result = json.loads(response.content)
    print("JSON parsed successfully")
    return result, response, prompt


def write_sample_texts(client: OllamaClient, extraction: dict, source_name: str, recorder):
    """Pass 2: title and abstract per sample, one short call each.

    Returns (texts, notes): texts[i] is None where writing failed, so that
    sample falls back to the copied text; notes are (index, issue) pairs the
    model flagged for a curator.
    """
    samples = extraction.get("samples") or []
    print(f"Writing titles and abstracts for {len(samples)} sample(s)...")
    texts, notes = [], []
    for i, sample in enumerate(samples):
        label = sample.get("sampleCode") or f"sample {i + 1}"
        prompt = get_sample_writing_prompt(extraction, i, source_name)
        try:
            response = client.chat(prompt, schema=SAMPLE_TEXT_SCHEMA, think=False)
            text = json.loads(response.content)
            recorder.record_pass(f"write sample {i + 1}", response, prompt)
            texts.append(text)
            timing = "cached" if response.cached else format_time(response.duration_s)
            print(f"  {i + 1:2d}. {label}: {text.get('title', '')[:70]}  ({timing})")
            notes += [(i, issue) for issue in text.get("issues") or [] if issue.strip()]
        except Exception as e:
            print(f"  {i + 1:2d}. {label}: [ERROR] {e}")
            texts.append(None)
    return texts, notes


def _safe_name(text: str, limit: int = 60) -> str:
    return "".join(c for c in text if c.isalnum() or c in (" ", "-", "_", ".")).strip()[:limit]


def save_igsn_data(documents):
    """One JSON list per source document, named after it, like the curated
    igsn-request-*.json files. documents: [(source_file, records, recorder)]"""
    print(f"\nSaving records of {len(documents)} document(s)...")

    saved_files = []
    for source_file, records, recorder in documents:
        output_file = JSON_OUTPUT_PATH / f"{_safe_name(source_file.stem)}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        saved_files.append(output_file)
        size = output_file.stat().st_size / 1024
        print(f"  {output_file.name} ({len(records)} record(s), {size:.2f} KB)")
        sidecar = recorder.write(output_file)
        print(f"     └─ {sidecar.name}")
    return saved_files


def generate_pdfs_from_igsn(documents, generated_by_ai=True):
    """One PDF per sample: <document>__<nn>_<sample title>.pdf"""
    total = sum(len(records) for _, records, _ in documents)
    print(f"\nGenerating PDFs for {total} record(s)...")

    saved_files = []
    for source_file, records, _ in documents:
        for idx, igsn_data in enumerate(records, 1):
            title = (igsn_data.get("titles") or [{}])[0].get("title", "")
            filename = f"{_safe_name(source_file.stem, 40)}__{idx:02d}_{_safe_name(title, 40)}.pdf"
            try:
                output_file = generate_igsn_pdf(igsn_data, filename, generated_by_ai=generated_by_ai)
                saved_files.append(output_file)
                print(f"  {filename} ({output_file.stat().st_size / 1024:.2f} KB)")
            except Exception as e:
                print(f"  [ERROR] Generating PDF for {filename}: {str(e)}")
    return saved_files

def main(model=None, host=None, use_cache=True, max_tokens=None, files=None, write=True, lookups=True):
    total_start = time.time()
    print("Starting IGSN data extraction...\n")

    client = OllamaClient(
        **{k: v for k, v in {"model": model, "host": host}.items() if v},
        use_cache=use_cache,
    )
    if max_tokens:
        client.options["num_predict"] = max_tokens
    curator = config.data_curator()
    lookup = enrich.Lookup(offline=not lookups)
    print(f"Model: {client.model}  |  Host: {client.host or 'local'}  |  Cache: {use_cache}"
          f"  |  Max tokens: {client.options.get('num_predict')}  |  Writing pass: {'on' if write else 'off'}")
    print(f"Data curator: {curator['name'] if curator else '[WARN] none configured (IGSN_CURATOR_NAME in .env)'}\n")

    if files:
        files = [Path(f) for f in files]
        print(f"Using {len(files)} file(s) given on the command line\n")
    else:
        print("Loading files from ./data...")
        files = get_all_unused_files()
        print(f"  └─ {len(files)} file(s) found\n")

    print("Loading documents...")
    convert_start = time.time()
    data = load_documents(files)
    convert_time = time.time() - convert_start
    print(f"  └─ {len(data)} document(s) loaded ({format_time(convert_time)})\n")

    print("=" * 50)
    processed = []  # (source_file, records, recorder)
    extraction_start = time.time()
    for idx, document in enumerate(data, 1):
        source_file, file_data = document.path, document.text
        print(f"\nProcessing document {idx}/{len(data)}: {source_file.name}")
        recorder = ProvenanceRecorder(source_file, file_data)
        recorder.record_loading(document)
        try:
            extraction, response, prompt = extract_data_to_json(client, file_data)
            recorder.record_pass("extract", response, prompt)

            # Pass 1 only copies; drop what is not in the document before
            # the writing pass builds prose on it.
            extraction, verify_changes = verify_extraction(extraction, file_data)
            recorder.record_changes(verify_changes)
            for change in verify_changes:
                print(f"  └─ {change}")

            texts, notes = (write_sample_texts(client, extraction, source_file.name, recorder)
                            if write else (None, []))

            records, changes = build_records(extraction, file_data, texts=texts, curator=curator)
            recorder.record_changes(changes)
            for change in changes:
                print(f"  └─ {change}")

            print(f"{len(records)} sample(s) found:")
            for i, record in enumerate(records, 1):
                titles = record.get("titles") or [{}]
                code = next((t["title"] for t in titles if t.get("titleType") == "AlternativeTitle"), "")
                print(f"  {i:2d}. {titles[0].get('title', 'N/A')}" + (f"  [{code}]" if code else ""))
            print(f"  └─ Authors: {len(records[0].get('creators', [])) if records else 0} people")

            print("Checking identifiers against the source document...")
            findings = validators.check_samples(records)
            findings += [validators.Finding("warning", "model-note", f"records[{i}]", "", issue)
                         for i, issue in notes]
            for i, record in enumerate(records):
                for finding in validators.validate(record, file_data):
                    finding.path = f"records[{i}].{finding.path}"
                    findings.append(finding)
            validators.print_findings(findings)
            recorder.record_findings(findings)

            # After validation: the checks above compare the record with the
            # document; ORCID and ROR data comes from outside it.
            records, lookup_changes, lookup_findings = enrich.enrich_records(records, lookup)
            recorder.record_changes(lookup_changes)
            recorder.record_findings(lookup_findings)
            recorder.enrichment = enrich.status(lookup)
            renamed = sum("(ORCID record)" in c for c in lookup_changes)
            with_ror = sum(c.startswith("affiliation ") for c in lookup_changes)
            print(f"ORCID/ROR lookups: {renamed} name(s) completed, {with_ror} affiliation(s) matched to ROR"
                  + (" [WARN] no connection, lookups skipped" if lookup.offline and lookups else "")
                  + ("" if lookups else " (disabled)"))
            for c in lookup_changes:
                if c.startswith("no unambiguous"):
                    print(f"  └─ {c}")
            for f in lookup_findings:
                print(f"  [WARN] {f.value}: {f.message}")

            report = benchmark.evaluate_document(source_file.name, records, file_data)
            if report:
                s = report["summary"]
                print(f"Compared with curated records ({report['curated']}):")
                print(f"  samples {report['samples']['matched']}/{report['samples']['expected']} matched"
                      + (f", {sum(p['uncertain'] for p in report['samples']['pairs'])} uncertain"
                         if any(p["uncertain"] for p in report["samples"]["pairs"]) else ""))
                print(f"  from the document {s['document']['passed']}/{s['document']['total']}"
                      f" ({s['document']['score']:.0%}), policy {s['policy']['passed']}/{s['policy']['total']},"
                      f" curator additions {s['curator']['total']} (not scored)")
                print("  details: uv run python benchmark.py --details")
                recorder.record_evaluation({"curated": report["curated"], "samples": report["samples"], **s})

            processed.append((source_file, records, recorder))
            print()
        except Exception as e:
            print(f"  [ERROR] {str(e)}\n")

    extraction_time = time.time() - extraction_start

    # Shared machine: give the VRAM back once the batch is done.
    client.unload()

    print("=" * 50)
    json_files = save_igsn_data(processed)

    pdf_start = time.time()
    pdf_files = generate_pdfs_from_igsn(processed)
    pdf_time = time.time() - pdf_start

    total_time = time.time() - total_start
    print(f"\nTiming:")
    print(f"  └─ Conversion: {format_time(convert_time)}")
    print(f"  └─ Extraction: {format_time(extraction_time)}")
    print(f"  └─ PDF generation: {format_time(pdf_time)}")
    print(f"  └─ Total: {format_time(total_time)}")
    print(f"\nDone.")

    return {
        'json_files': json_files,
        'pdf_files': pdf_files
    }

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Extract IGSN metadata from ./data")
    parser.add_argument("--model", help="Ollama model tag (default: qwen3:14b)")
    parser.add_argument("--host", help="Ollama host (default: OLLAMA_HOST or local)")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached model responses")
    parser.add_argument("--max-tokens", type=int, help="Stop generation after this many tokens (default: 8192)")
    parser.add_argument("--no-write", action="store_true",
                        help="Skip the writing pass; titles/abstracts come from the document text")
    parser.add_argument("--no-lookup", action="store_true",
                        help="Skip ORCID/ROR lookups (enrich later with enrich.py)")
    parser.add_argument("files", nargs="*", help="Documents to process (default: all in ./data)")
    args = parser.parse_args()

    result = main(model=args.model, host=args.host, use_cache=not args.no_cache,
                  max_tokens=args.max_tokens, files=args.files, write=not args.no_write,
                  lookups=not args.no_lookup)
    print(f"\nOutput files:")
    print(f"  JSON files: {len(result['json_files'])}")
    for f in result['json_files']:
        print(f"    └─ {f}")
    print(f"  PDF files: {len(result['pdf_files'])}")
    for f in result['pdf_files']:
        print(f"    └─ {f}")
