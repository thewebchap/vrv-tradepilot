"""Comparing two documents and wording the result. No Outlook code here.

``validate_pdfs`` takes plain (filename, bytes) pairs, so the same logic can later be fed
from any source, not only email. Field values may come from the PDF parser or, as a
fallback, from the local document model (extraction.py). The decision PASS / FAIL /
REVIEW_REQUIRED is always made here, by deterministic code.
"""

from __future__ import annotations

import logging

from extraction import extract_document
from local_model import LocalDocumentModel
from pdf_parser import (
    AMBIGUOUS,
    CONFLICT,
    FOUND,
    DocumentError,
    FieldResult,
    FieldsConfig,
    values_equal,
)

log = logging.getLogger(__name__)

PASS, FAIL, REVIEW_REQUIRED = "PASS", "FAIL", "REVIEW_REQUIRED"
_PROBLEM = {AMBIGUOUS: "is ambiguous", CONFLICT: "was read differently by the PDF parser and the local model"}


def review(issue: str) -> dict:
    return {"status": REVIEW_REQUIRED, "matches": [], "mismatches": [], "issues": [issue]}


def _plain(value):
    """JSON-friendly form of a normalized value (Amount and date become text)."""
    return value if value is None or isinstance(value, int | str) else str(value)


def _display(r: FieldResult):
    return f"{_plain(r.value)} {r.unit}" if r.unit else _plain(r.value)


def compare(
    doc1: str, fields1: dict[str, FieldResult], doc2: str, fields2: dict[str, FieldResult], config: FieldsConfig
) -> dict:
    result = {"status": PASS, "matches": [], "mismatches": [], "issues": []}
    for spec in config.fields:
        a, b = fields1[spec.name], fields2[spec.name]
        problems = [(doc, r) for doc, r in ((doc1, a), (doc2, b)) if r.status != FOUND]
        if problems:
            for doc, r in problems:
                if spec.required or r.status == CONFLICT:  # a conflict is never ignored
                    problem = _PROBLEM.get(r.status, "could not be identified")
                    result["issues"].append(f'Required field "{spec.title}" {problem} in {doc}.')
            continue  # a missing optional field is allowed
        entry = {"field": spec.name, "title": spec.title, "document_1": _display(a), "document_2": _display(b)}
        same = values_equal(a.value, b.value, spec.type, a.unit, b.unit)
        result["matches" if same else "mismatches"].append(entry)

    if result["issues"]:
        result["status"] = REVIEW_REQUIRED
    elif result["mismatches"]:
        result["status"] = FAIL
    return result


def _audit(name: str, fields: dict[str, FieldResult]) -> dict:
    """What is stored in SQLite for each field: raw, normalized, unit, source, status."""
    return {
        "file": name,
        "fields": {
            k: {"raw": r.raw, "normalized": _plain(r.value), "unit": r.unit, "source": r.source, "status": r.status}
            for k, r in fields.items()
        },
    }


def validate_pdfs(pdfs: list[tuple[str, bytes]], config: FieldsConfig, model: LocalDocumentModel | None = None) -> dict:
    """Extract (PDF parser first, local model only when needed) and compare the first two PDFs."""
    if len(pdfs) < 2:
        return review(f"Expected two PDF attachments, found {len(pdfs)}.")
    if len(pdfs) > 2:
        log.warning("%d PDFs attached; only the first two are compared", len(pdfs))

    extracted, problems = [], []
    for name, content in pdfs[:2]:
        try:
            fields, doc_problems = extract_document(name, content, config, model)
        except DocumentError as exc:
            return review(f"{name}: {exc}.")
        except Exception:  # any extraction failure becomes REVIEW REQUIRED instead of a crash
            log.exception("Could not process %s", name)
            return review(f"{name}: the PDF could not be processed.")
        log.info(
            "Fields in %s: %s",
            name,
            {k: (f"{r.raw} [{r.source}]" if r.status == FOUND else r.status) for k, r in fields.items()},
        )
        extracted.append((name, fields))
        problems += doc_problems

    (doc1, fields1), (doc2, fields2) = extracted
    result = compare(doc1, fields1, doc2, fields2, config)
    if problems:
        result["issues"] = problems + result["issues"]
        result["status"] = REVIEW_REQUIRED
    result["local_model_fields"] = [
        f"{s.title} ({name})"
        for name, fields in extracted
        for s in config.fields
        if fields[s.name].status == FOUND and fields[s.name].source == "local_model"
    ]
    result["documents"] = [_audit(name, fields) for name, fields in extracted]
    return result


def format_reply(result: dict) -> str:
    if result["status"] == PASS:
        lines = ["VRV TradePilot validation completed.", "", "All configured checks passed.", ""]
    elif result["status"] == FAIL:
        lines = ["VRV TradePilot found the following differences:", ""]
        for m in result["mismatches"]:
            lines += [m["title"], f"Document 1: {m['document_1']}", f"Document 2: {m['document_2']}", ""]
    else:
        lines = ["VRV TradePilot could not complete the automatic validation.", "", "Reason:", *result["issues"], ""]
    if result.get("local_model_fields"):
        lines += [f"Note: read by the local document model: {', '.join(result['local_model_fields'])}.", ""]
    lines.append("Status: GOOD TO GO" if result["status"] == PASS else "Status: REVIEW REQUIRED")
    return "\n".join(lines)
