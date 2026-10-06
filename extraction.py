"""Hybrid field extraction for one document: deterministic first, local model only if needed.

1. Run the deterministic PyMuPDF extractor (pdf_parser).
2. If every required field was FOUND, use that result. The model is not called.
3. Otherwise ask the local model (if enabled) to read the document and merge carefully:
   - a deterministic FOUND value is never overwritten; if the model reads something
     different, the field becomes CONFLICT (=> REVIEW REQUIRED);
   - a missing field is filled with the model's value only if it normalizes cleanly and,
     when the PDF has a text layer, the value actually appears in that text;
   - an AMBIGUOUS field is resolved only if the model picked one of the values the
     deterministic extractor itself found.
   Each field records its source: "deterministic" or "local_model".

The result is plain field data; deciding PASS / FAIL stays in compare.py.
"""

from __future__ import annotations

import logging
import re

from local_model import LocalDocumentModel, LocalModelError
from pdf_parser import (
    AMBIGUOUS,
    CONFLICT,
    FOUND,
    NOT_FOUND,
    FieldResult,
    FieldsConfig,
    FieldSpec,
    NoTextError,
    extract_fields,
    normalize,
    read_words,
    split_unit,
    values_equal,
)

log = logging.getLogger(__name__)


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def extract_document(
    name: str, pdf: bytes, config: FieldsConfig, model: LocalDocumentModel | None
) -> tuple[dict[str, FieldResult], list[str]]:
    """Return (fields, problems). Problems are document-level reasons for REVIEW REQUIRED.

    Raises pdf_parser.DocumentError for files that cannot be used at all.
    """
    use_model = model is not None and config.use_local_model_fallback
    try:
        words = read_words(pdf)
    except NoTextError:
        if not use_model:
            raise
        log.info("%s has no text layer; reading it with the local model", name)
        words = []

    fields = (
        extract_fields(words, config)
        if words
        else {s.name: FieldResult(s.name, NOT_FOUND, detail="no text layer") for s in config.fields}
    )
    weak = [s.title for s in config.fields if s.required and fields[s.name].status != FOUND]
    if not weak or not use_model:
        return fields, []

    log.info("%s: %s not reliably found; asking the local model", name, ", ".join(weak))
    try:
        model_values = model.extract(pdf, config.fields)
    except LocalModelError as exc:
        log.error("Local model failed for %s: %s", name, exc)
        return fields, [f"{name}: the local document model could not be used ({exc})."]

    text = _compact(" ".join(w.text for w in words)) if words else None
    for spec in config.fields:
        fields[spec.name] = _merge(spec, fields[spec.name], model_values.get(spec.name), text, config)
    return fields, []


def _merge(spec: FieldSpec, det: FieldResult, raw: str | None, text: str | None, config: FieldsConfig) -> FieldResult:
    if raw is None:
        return det  # the model found nothing: keep the deterministic outcome

    try:
        value = normalize(raw, spec.type, config)
        if spec.pattern and not re.fullmatch(spec.pattern, str(value)):
            raise ValueError(f"does not match pattern {spec.pattern!r}")
    except ValueError as exc:
        if det.status == FOUND:
            return det
        return FieldResult(spec.name, det.status, detail=f"local model value not usable: {exc}", source="local_model")

    unit = split_unit(raw)[1] if spec.type == "integer" else None
    if det.status == FOUND:
        if values_equal(det.value, value, spec.type, det.unit, unit):
            return det
        log.warning("%s: deterministic %r and local model %r disagree", spec.name, det.raw, raw)
        return FieldResult(
            spec.name,
            CONFLICT,
            detail=f"PDF parser read {det.raw!r} but the local model read {raw!r}",
            source="deterministic+local_model",
        )

    if det.status == AMBIGUOUS and str(value) not in (det.candidates or []):
        return FieldResult(spec.name, AMBIGUOUS, detail=f"{det.detail}; local model read {raw!r}", source="local_model")

    if text is not None and _compact(raw) not in text:  # guard against invented values
        return FieldResult(spec.name, NOT_FOUND, detail=f"local model value {raw!r} does not appear in the PDF text", source="local_model")

    return FieldResult(spec.name, FOUND, raw, value, source="local_model", unit=unit)
