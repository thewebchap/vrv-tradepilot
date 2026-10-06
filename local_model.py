"""Optional local document model, used only to READ field values from a PDF.

The model never decides whether documents match: it returns the printed text of each
configured field (or null), and TradePilot's normal normalization and deterministic
comparison take it from there.

Provider: Ollama (https://ollama.com), running on this machine. PDF pages are rendered to
PNG images locally with PyMuPDF and sent, together with the PDF's text layer if it has
one, to Ollama's local HTTP API. Nothing leaves the machine.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import httpx
import pymupdf

from pdf_parser import FieldSpec

log = logging.getLogger(__name__)

MAX_PAGES = 3  # pages sent to the model (first pages of the document)
RENDER_DPI = 144
MAX_TEXT_CHARS = 8000


class LocalModelError(Exception):
    """The model is unavailable, too slow, or returned unusable output."""


class LocalDocumentModel:
    def __init__(self, model: str, base_url: str = "http://localhost:11434", timeout: float = 180) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> LocalDocumentModel | None:
        """The configured model, or None when LOCAL_MODEL_ENABLED is not true."""
        if os.environ.get("LOCAL_MODEL_ENABLED", "false").strip().lower() != "true":
            return None
        provider = os.environ.get("LOCAL_MODEL_PROVIDER", "ollama").strip().lower()
        if provider != "ollama":
            raise LocalModelError(f"LOCAL_MODEL_PROVIDER={provider!r} is not supported (only 'ollama')")
        name = os.environ.get("LOCAL_MODEL_NAME", "").strip()
        if not name:
            raise LocalModelError("LOCAL_MODEL_ENABLED=true but LOCAL_MODEL_NAME is empty")
        return cls(
            name,
            os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            float(os.environ.get("LOCAL_MODEL_TIMEOUT_SECONDS") or 180),
        )

    def check(self) -> None:
        """Raise LocalModelError if Ollama is not running or the model is not pulled."""
        try:
            tags = httpx.get(f"{self.base_url}/api/tags", timeout=5).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalModelError(f"Ollama is not reachable at {self.base_url} ({type(exc).__name__})") from exc
        names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            raise LocalModelError(f"model {self.model!r} is not pulled; run: ollama pull {self.model}")
        try:
            capabilities = httpx.post(f"{self.base_url}/api/show", json={"model": self.model}, timeout=5).json().get("capabilities")
        except (httpx.HTTPError, ValueError):
            capabilities = None
        if capabilities is not None and "vision" not in capabilities:
            # Ollama silently ignores images for such models: only the PDF text layer is used.
            log.warning("Local model %s has no vision support: scanned PDFs cannot be read with it", self.model)

    # ---------------------------------------------------------------------------------

    def extract(self, pdf: bytes, fields: list[FieldSpec]) -> dict[str, str | None]:
        """Return {field_name: printed value or None} for every configured field."""
        images, text = _render(pdf)
        prompt = _prompt(fields, text)
        for attempt in (1, 2):
            content = self._chat(prompt, images, fields)
            try:
                return _validate(content, fields)
            except ValueError as exc:
                log.warning("Local model returned invalid output (attempt %d): %s", attempt, exc)
                prompt = _prompt(fields, text, strict=True)
        raise LocalModelError("the local model returned invalid JSON twice")

    def _chat(self, prompt: str, images: list[str], fields: list[FieldSpec]) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt, "images": images}],
            "stream": False,
            "format": _schema(fields),  # Ollama constrains the output to this JSON schema
            "options": {"temperature": 0},
        }
        try:
            response = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise LocalModelError(f"the local model did not answer within {self.timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise LocalModelError(f"Ollama is not reachable at {self.base_url} ({type(exc).__name__})") from exc
        if response.status_code == 404:
            raise LocalModelError(f"model {self.model!r} is not available; run: ollama pull {self.model}")
        if response.status_code >= 400:
            raise LocalModelError(f"Ollama returned {response.status_code}: {response.text[:200]}")
        try:
            return response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LocalModelError("unexpected response from Ollama") from exc


# --------------------------------------------------------------------------- helpers


def _render(pdf: bytes) -> tuple[list[str], str]:
    """First pages as base64 PNG images, plus the PDF's text layer (may be empty)."""
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as exc:  # damaged file
        raise LocalModelError(f"the PDF could not be rendered ({type(exc).__name__})") from exc
    with doc:
        pages = list(doc)[:MAX_PAGES]
        images = [base64.b64encode(p.get_pixmap(dpi=RENDER_DPI).tobytes("png")).decode() for p in pages]
        text = "\n".join(p.get_text() for p in pages).strip()[:MAX_TEXT_CHARS]
    return images, text


def _schema(fields: list[FieldSpec]) -> dict:
    return {
        "type": "object",
        "properties": {f.name: {"type": ["string", "null"]} for f in fields},
        "required": [f.name for f in fields],
        "additionalProperties": False,
    }


def _prompt(fields: list[FieldSpec], text: str, strict: bool = False) -> str:
    lines = [
        "You read business documents (invoices, purchase orders, packing lists).",
        "Extract ONLY these fields from the attached document images:",
    ]
    for f in fields:
        lines.append(f'- "{f.name}": {f.title} ({f.type}); may be labelled {", ".join(repr(x) for x in f.labels)}')
    lines += [
        "",
        "Rules:",
        "- Copy each value exactly as printed in the document (keep units, currency symbols and separators).",
        "- Do not calculate, convert, translate or guess. If a field is not clearly present, use null.",
        "- If a field appears several times with different values, use null.",
        "- Answer with one JSON object containing exactly these keys and nothing else.",
    ]
    if strict:
        lines.append("- Your previous answer was not valid. Output ONLY the JSON object: no text, no markdown.")
    if text:
        lines += ["", "Text layer of the document (may be incomplete or out of order):", text]
    return "\n".join(lines)


def _validate(content: str, fields: list[FieldSpec]) -> dict[str, str | None]:
    """Strictly check the model's answer: a JSON object with exactly the configured keys,
    each a string, a plain number or null. Raises ValueError otherwise."""
    data = json.loads(content)  # json.loads only parses data; nothing is executed
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    expected = {f.name for f in fields}
    if set(data) != expected:
        raise ValueError(f"unexpected keys: {sorted(set(data) ^ expected)}")
    result: dict[str, str | None] = {}
    for name, value in data.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            result[name] = None
        elif isinstance(value, str):
            result[name] = value.strip()
        elif isinstance(value, int | float) and not isinstance(value, bool):
            result[name] = str(value)
        else:
            raise ValueError(f"{name}: unsupported value type {type(value).__name__}")
    return result
