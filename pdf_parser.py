"""PDF field extraction and value normalization. No Outlook code here.

How a field is found:

1. Read every word with its coordinates (PyMuPDF ``page.get_text("words")``).
2. Group words into visual lines and split each line into phrases at wide gaps, so
   ``Quantity        20`` becomes two phrases: "Quantity" and "20".
3. Find the field's labels. Multi-word labels ("Purchase Order Number") are matched across
   consecutive words; the longest label wins ("Total Amount" over "Amount").
4. Next to each label occurrence, look for a value in this order:
       same row to the right  ->  directly below  ->  below and to the right
   and take the nearest phrase that is a valid value for the field's type.
   A value is never taken from beyond another label.
5. Labels are tried in the configured order. If one label appears several times with
   different values, the field is AMBIGUOUS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import pymupdf

MIN_TEXT_CHARS = 20  # less extractable text than this: probably a scanned image
PHRASE_GAP = 0.8  # horizontal gap (x word height) that separates two phrases
MAX_BELOW = 60  # points: how far below a label a value may be (~4 lines)
MAX_RIGHT = 300  # points: how far right of a label a value may be

FOUND, NOT_FOUND, AMBIGUOUS, CONFLICT = "FOUND", "NOT_FOUND", "AMBIGUOUS", "CONFLICT"


class DocumentError(Exception):
    """The PDF cannot be used (not a PDF, password protected, no text...)."""


class NoTextError(DocumentError):
    """The PDF is valid but has no text layer (e.g. a scanned image)."""


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class FieldSpec:
    name: str
    labels: list[str]
    type: str = "string"  # string | integer | decimal | date
    required: bool = True
    pattern: str | None = None
    title: str = ""


@dataclass(frozen=True)
class FieldsConfig:
    fields: list[FieldSpec]
    date_formats: list[str]
    currency_symbols: dict[str, str]
    use_local_model_fallback: bool = False


def load_fields(config: dict) -> FieldsConfig:
    """Build field definitions from the parsed config.yaml."""
    specs = []
    for name, item in (config.get("fields") or {}).items():
        if item.get("type", "string") not in ("string", "integer", "decimal", "date"):
            raise ValueError(f"config.yaml: field {name!r} has unknown type {item.get('type')!r}")
        specs.append(
            FieldSpec(
                name=name,
                labels=[str(label) for label in item["labels"]],
                type=item.get("type", "string"),
                required=bool(item.get("required", True)),
                pattern=item.get("pattern"),
                title=item.get("title") or name.replace("_", " ").title(),
            )
        )
    if not specs:
        raise ValueError("config.yaml defines no fields")
    return FieldsConfig(
        specs,
        list(config.get("date_formats") or []),
        dict(config.get("currency_symbols") or {}),
        bool((config.get("extraction") or {}).get("use_local_model_fallback", False)),
    )


# --------------------------------------------------------------------------- normalization


@dataclass(frozen=True)
class Amount:
    value: Decimal
    currency: str | None = None

    def __str__(self) -> str:
        return f"{self.currency} {self.value}" if self.currency else str(self.value)


_GROUPED = re.compile(r"^\d{1,3}([, ]\d{3})+$")
_UNIT = re.compile(r"^(.*\d)\s*([A-Za-z]{1,5})\.?$")
_CURRENCY_CODE = re.compile(r"^([A-Z]{3})\s*(?=[\d(+\-.])|(?<=[\d)])\s*([A-Z]{3})$")


def clean(text: str) -> str:
    """Trim, collapse whitespace, drop a leading ':' or '#' left over from a label."""
    return re.sub(r"\s+", " ", text).strip().lstrip(":#").strip()


def to_number(text: str) -> Decimal:
    """1000 / 1,000 / 1 000 / 12,500.00 -> Decimal. Comma or space as thousands separator."""
    text = clean(text)
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = text.strip("()-+ ")
    whole, _, fraction = text.partition(".")
    if not (whole.isdigit() or _GROUPED.match(whole)) or (fraction and not fraction.isdigit()):
        raise ValueError(f"not a number: {text!r}")
    try:
        number = Decimal(whole.replace(",", "").replace(" ", "") + (f".{fraction}" if fraction else ""))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {text!r}") from exc
    return -number if negative else number


def split_unit(text: str) -> tuple[str, str | None]:
    """'25 MT' -> ('25', 'MT'); '1,000' -> ('1,000', None)."""
    match = _UNIT.match(clean(text))
    return (match.group(1), match.group(2).upper()) if match else (clean(text), None)


def to_integer(text: str) -> int:
    """1,000 / 1000 / 1 000 -> 1000. A trailing unit ('25 MT') is allowed; see split_unit."""
    number = to_number(split_unit(text)[0])
    if number != number.to_integral_value():
        raise ValueError(f"not a whole number: {text!r}")
    return int(number)


def to_amount(text: str, currency_symbols: dict[str, str]) -> Amount:
    """12500 / 12,500.00 / USD 12,500 / $12,500 / 12,500 USD. No currency conversion."""
    text = clean(text)
    currency = None
    for symbol, code in currency_symbols.items():
        if text.startswith(symbol) or text.endswith(symbol):
            currency, text = code, text.removeprefix(symbol).removesuffix(symbol).strip()
            break
    if currency is None and (match := _CURRENCY_CODE.search(text)):
        currency = match.group(1) or match.group(2)
        text = (text[: match.start()] + text[match.end() :]).strip()
    return Amount(to_number(text), currency)


def to_date(text: str, formats: list[str]) -> date:
    """Only the configured formats are tried; a text that fits several formats with
    different results is ambiguous and rejected (never guessed)."""
    text = clean(text).rstrip(".")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)  # YYYY-MM-DD is never ambiguous
    return _strptime(text, formats)


def _strptime(text: str, formats: list[str]) -> date:
    results = set()
    for fmt in formats:
        try:
            results.add(datetime.strptime(text, fmt).date())
        except ValueError:
            pass
    if len(results) > 1:
        raise ValueError(f"ambiguous date: {text!r}")
    if not results:
        raise ValueError(f"unsupported date format: {text!r}")
    return results.pop()


def values_equal(a, b, field_type: str, unit_a: str | None = None, unit_b: str | None = None) -> bool:
    """Equality of two normalized values (strings ignore case; amounts in different
    currencies and quantities in different units are never equal; nothing is converted)."""
    if unit_a and unit_b and unit_a != unit_b:
        return False
    if field_type == "string":
        return a.casefold() == b.casefold()
    if field_type == "decimal":
        if a.currency and b.currency and a.currency != b.currency:
            return False
        return a.value == b.value
    return a == b


def normalize(text: str, field_type: str, config: FieldsConfig):
    if field_type == "integer":
        return to_integer(text)
    if field_type == "decimal":
        return to_amount(text, config.currency_symbols)
    if field_type == "date":
        return to_date(text, config.date_formats)
    value = clean(text)
    if not value:
        raise ValueError("empty value")
    return value


# --------------------------------------------------------------------------- PDF words


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int


class Phrase:
    """A group of words (a value candidate or a matched label)."""

    def __init__(self, words: list[Word], line: int, field: str = "", label: str = "") -> None:
        self.words, self.line, self.field, self.label = words, line, field, label
        self.page = words[0].page
        self.x0 = min(w.x0 for w in words)
        self.y0 = min(w.y0 for w in words)
        self.x1 = max(w.x1 for w in words)
        self.y1 = max(w.y1 for w in words)
        self.text = " ".join(w.text for w in words)


def read_words(pdf: bytes) -> list[Word]:
    """Every word with its bounding box and 1-based page number."""
    if not pdf.lstrip()[:5] == b"%PDF-":
        raise DocumentError("the file is not a PDF")
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several error types for damaged files
        raise DocumentError(f"the PDF could not be opened ({type(exc).__name__})") from exc
    with doc:
        if doc.needs_pass:
            raise DocumentError("the PDF is password protected")
        words = [
            Word(text, x0, y0, x1, y1, page_no)
            for page_no, page in enumerate(doc, start=1)
            for x0, y0, x1, y1, text, *_ in page.get_text("words")
            if text.strip()
        ]
    if sum(len(w.text) for w in words) < MIN_TEXT_CHARS:
        raise NoTextError("the PDF has no extractable text (it may be a scanned image; OCR is not supported yet)")
    return words


# --------------------------------------------------------------------------- layout


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def group_lines(words: list[Word]) -> list[list[Word]]:
    """Words that overlap vertically on the same page form a line, sorted left to right."""
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.page, (w.y0 + w.y1) / 2, w.x0)):
        if lines:
            first = lines[-1][0]
            height = min(first.y1 - first.y0, word.y1 - word.y0) or 1
            if first.page == word.page and _overlap(first.y0, first.y1, word.y0, word.y1) / height >= 0.5:
                lines[-1].append(word)
                continue
        lines.append([word])
    for line in lines:
        line.sort(key=lambda w: w.x0)
    return lines


def _compact(text: str) -> str:
    return re.sub(r"[\s:.]+", "", text.casefold())


def find_labels(lines: list[list[Word]], fields: list[FieldSpec]) -> list[Phrase]:
    hits: list[Phrase] = []
    for line_no, line in enumerate(lines):
        tokens = [_compact(w.text) for w in line]
        for spec in fields:
            for label in spec.labels:
                target = _compact(label)
                for start in range(len(line)):
                    text = ""
                    for end in range(start, len(line)):
                        if end > start and line[end].x0 - line[end - 1].x1 > 2 * (line[end].y1 - line[end].y0):
                            break  # the words of one label are never far apart
                        text += tokens[end]
                        if text == target:
                            hits.append(Phrase(line[start : end + 1], line_no, spec.name, label))
                        if not target.startswith(text):
                            break
    # Where labels overlap, the longest wins ("Total Amount" beats "Amount" and "Total").
    hits.sort(key=lambda h: (-len(h.words), -len(h.label)))
    kept, used = [], set()
    for hit in hits:
        ids = {id(w) for w in hit.words}
        if not ids & used:
            kept.append(hit)
            used |= ids
    return kept


def split_phrases(lines: list[list[Word]], labels: list[Phrase]) -> list[Phrase]:
    """Value candidates: runs of non-label words, split at wide gaps."""
    label_words = {id(w) for hit in labels for w in hit.words}
    phrases: list[Phrase] = []
    for line_no, line in enumerate(lines):
        current: list[Word] = []
        for i, word in enumerate(line):
            wide_gap = i > 0 and word.x0 - line[i - 1].x1 > PHRASE_GAP * (word.y1 - word.y0)
            if id(word) in label_words or wide_gap:
                if current:
                    phrases.append(Phrase(current, line_no))
                current = []
            if id(word) not in label_words and (current or word.text.strip(":#-|")):
                current.append(word)
        if current:
            phrases.append(Phrase(current, line_no))
    return phrases


# --------------------------------------------------------------------------- extraction


@dataclass
class FieldResult:
    field: str
    status: str  # FOUND | NOT_FOUND | AMBIGUOUS | CONFLICT
    raw: str | None = None
    value: object = None
    detail: str | None = None
    source: str = "deterministic"  # deterministic | local_model
    unit: str | None = None  # integer fields, e.g. "MT" from "25 MT"
    candidates: list[str] | None = None  # the different values found when AMBIGUOUS


def _candidates(hit: Phrase, phrases: list[Phrase], labels: list[Phrase]):
    """Phrases near a label, in order of preference: same row, below, below-right."""
    nearby = [p for p in phrases if p.page == hit.page]
    others = [h for h in labels if h is not hit and h.page == hit.page]

    def blocked_right(p: Phrase) -> bool:  # another label sits between label and value
        return any(h.line == hit.line and hit.x1 <= h.x0 and h.x1 <= p.x0 for h in others)

    def blocked_below(p: Phrase) -> bool:
        return any(h.y0 >= hit.y1 - 1 and h.y1 <= p.y0 + 1 and _overlap(h.x0, h.x1, p.x0, p.x1) > 0 for h in others)

    same_row = [p for p in nearby if p.line == hit.line and p.x0 >= hit.x1 - 1 and p.x0 - hit.x1 <= MAX_RIGHT]
    for p in sorted(same_row, key=lambda p: p.x0):
        if not blocked_right(p):
            yield p

    below_zone = [p for p in nearby if p.line != hit.line and p.y0 >= hit.y1 - 2 and p.y0 - hit.y1 <= MAX_BELOW]
    below = [p for p in below_zone if _overlap(hit.x0, hit.x1, p.x0, p.x1) > 0]
    for p in sorted(below, key=lambda p: p.y0):
        if not blocked_below(p):
            yield p

    below_right = [p for p in below_zone if p.x0 >= hit.x1 - 1 and p.x0 - hit.x1 <= MAX_RIGHT]
    for p in sorted(below_right, key=lambda p: (p.y0 - hit.y1) + (p.x0 - hit.x1)):
        if not blocked_below(p):
            yield p


def _value_near(hit: Phrase, spec: FieldSpec, phrases, labels, config: FieldsConfig):
    for phrase in _candidates(hit, phrases, labels):
        try:
            value = normalize(phrase.text, spec.type, config)
        except ValueError:
            continue  # not a sensible value for this type: try the next candidate
        if spec.pattern and not re.fullmatch(spec.pattern, str(value)):
            continue
        return phrase.text, value
    return None


def extract_fields(words: list[Word], config: FieldsConfig) -> dict[str, FieldResult]:
    lines = group_lines(words)
    labels = find_labels(lines, config.fields)
    phrases = split_phrases(lines, labels)
    results = {}
    for spec in config.fields:
        result = FieldResult(spec.name, NOT_FOUND, detail="label not found")
        for label in spec.labels:
            hits = [h for h in labels if h.field == spec.name and h.label == label]
            found = [v for h in hits if (v := _value_near(h, spec, phrases, labels, config))]
            if hits and not found:
                result.detail = f'label "{hits[0].text}" found but no valid {spec.type} value next to it'
            if found:
                values = {str(value) for _, value in found}
                if len(values) > 1:
                    result = FieldResult(spec.name, AMBIGUOUS, detail=f"different values: {', '.join(sorted(values))}")
                    result.candidates = sorted(values)
                else:
                    raw, value = found[0]
                    unit = split_unit(raw)[1] if spec.type == "integer" else None
                    result = FieldResult(spec.name, FOUND, raw, value, unit=unit)
                break
        results[spec.name] = result
    return results
