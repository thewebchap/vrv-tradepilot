"""Local-model fallback. The model is always mocked: no Ollama needed to run pytest."""

import json

import pymupdf
import pytest
from conftest import make_pdf, side_by_side_doc, stacked_doc

import local_model
from compare import format_reply, validate_pdfs
from local_model import LocalDocumentModel, LocalModelError


class FakeModel:
    """Stands in for LocalDocumentModel: returns fixed values, or raises."""

    def __init__(self, values=None, error=None):
        self.values, self.error, self.calls = values or {}, error, 0

    def extract(self, pdf, fields):
        self.calls += 1
        if self.error:
            raise self.error
        return {f.name: self.values.get(f.name) for f in fields}


def no_quantity_doc(quantity="20 MT"):
    """A layout the parser can't read for Quantity: the label is far away from the value."""
    return make_pdf(("Invoice Number:", 50, 100), ("INV-1024", 220, 100), ("PO Number:", 50, 120), ("PO-7781", 220, 120),
                    ("Total Amount", 50, 140), ("USD 12,500.00", 220, 140), ("Currency", 50, 160), ("USD", 220, 160),
                    ("Shipped quantity is shown below", 50, 300), (quantity, 450, 500))  # fmt: skip


def run(config, second_pdf, model):
    return validate_pdfs([("invoice.pdf", side_by_side_doc()), ("po.pdf", second_pdf)], config, model)


def test_deterministic_success_does_not_call_model(config):
    model = FakeModel({"quantity": "999"})
    result = run(config, stacked_doc(), model)
    assert result["status"] == "PASS" and model.calls == 0
    assert result["local_model_fields"] == []


def test_missing_required_field_calls_model_and_model_fills_it(config):
    model = FakeModel({"quantity": "20 MT"})
    result = run(config, no_quantity_doc(), model)
    assert model.calls == 1
    assert result["status"] == "PASS", result
    po_fields = result["documents"][1]["fields"]
    assert po_fields["quantity"] == {"raw": "20 MT", "normalized": 20, "unit": "MT", "source": "local_model", "status": "FOUND"}
    assert po_fields["invoice_number"]["source"] == "deterministic"
    assert "Note: read by the local document model: Quantity (po.pdf)." in format_reply(result)


def test_model_values_are_compared_deterministically(config):
    """The model only reads '25'; compare.py decides that 20 != 25."""
    result = run(config, no_quantity_doc("25 MT"), FakeModel({"quantity": "25 MT"}))
    assert result["status"] == "FAIL"
    assert result["mismatches"][0]["field"] == "quantity"
    assert (result["mismatches"][0]["document_1"], result["mismatches"][0]["document_2"]) == (20, "25 MT")


def test_conflict_between_parser_and_model_is_review_required(config):
    model = FakeModel({"quantity": "20 MT", "invoice_number": "INV-9999"})  # parser read INV-1024
    result = run(config, no_quantity_doc(), model)
    assert result["status"] == "REVIEW_REQUIRED"
    assert any("Invoice Number" in i and "read differently" in i for i in result["issues"])
    assert result["documents"][1]["fields"]["invoice_number"]["status"] == "CONFLICT"


def test_invented_value_not_in_pdf_text_is_rejected(config):
    result = run(config, no_quantity_doc(), FakeModel({"quantity": "35"}))  # "35" is not in the PDF
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["documents"][1]["fields"]["quantity"]["status"] == "NOT_FOUND"


def test_model_cannot_invent_a_value_for_an_ambiguous_field(config):
    ambiguous = make_pdf(*[(t, x, y) for t, x, y in [("Invoice Number:", 50, 100), ("INV-1024", 220, 100),
                         ("PO Number:", 50, 120), ("PO-7781", 220, 120), ("Total Amount", 50, 140), ("USD 12,500.00", 220, 140),
                         ("Quantity", 50, 160), ("20", 220, 160), ("Quantity", 50, 400), ("25", 220, 400)]])  # fmt: skip
    assert run(config, ambiguous, FakeModel({"quantity": "30"}))["status"] == "REVIEW_REQUIRED"
    assert run(config, ambiguous, FakeModel({"quantity": "20"}))["status"] == "PASS"  # one of the values found


def test_model_unavailable_gives_review_required(config):
    model = FakeModel(error=LocalModelError("Ollama is not reachable at http://localhost:11434"))
    result = run(config, no_quantity_doc(), model)
    assert result["status"] == "REVIEW_REQUIRED"
    assert any("local document model could not be used" in i for i in result["issues"])


def test_real_client_with_ollama_down_does_not_crash(config):
    model = LocalDocumentModel("qwen2.5vl:7b", base_url="http://127.0.0.1:9", timeout=2)
    assert run(config, no_quantity_doc(), model)["status"] == "REVIEW_REQUIRED"


def test_local_model_disabled_works_as_before(config, monkeypatch):
    monkeypatch.delenv("LOCAL_MODEL_ENABLED", raising=False)
    assert LocalDocumentModel.from_env() is None
    result = run(config, no_quantity_doc(), None)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["issues"] == ['Required field "Quantity" could not be identified in po.pdf.']


def test_config_switch_disables_fallback(config):
    import dataclasses

    off = dataclasses.replace(config, use_local_model_fallback=False)
    model = FakeModel({"quantity": "20 MT"})
    assert run(off, no_quantity_doc(), model)["status"] == "REVIEW_REQUIRED" and model.calls == 0


def test_scanned_pdf_is_read_by_model(config):
    scanned = pymupdf.open()
    scanned.new_page()
    model = FakeModel({"invoice_number": "INV-1024", "po_number": "PO-7781", "quantity": "20", "amount": "USD 12,500.00"})
    result = run(config, scanned.tobytes(), model)
    assert result["status"] == "PASS" and model.calls == 1


# --- the Ollama client itself (HTTP mocked) -------------------------------------------


@pytest.fixture
def ollama(monkeypatch):
    answers, requests = [], []

    class Response:
        def __init__(self, content):
            self.status_code, self._content = 200, content

        def json(self):
            return {"message": {"content": self._content}}

    def fake_post(url, json, timeout):
        requests.append(json)
        return Response(answers.pop(0))

    monkeypatch.setattr(local_model.httpx, "post", fake_post)
    return answers, requests


def test_invalid_json_is_retried_once_then_accepted(config, ollama):
    answers, requests = ollama
    good = {f.name: None for f in config.fields} | {"quantity": "20 MT"}
    answers += ["Sure! Here is the data: {quantity: 20}", json.dumps(good)]
    values = LocalDocumentModel("m").extract(side_by_side_doc(), config.fields)
    assert values["quantity"] == "20 MT" and len(requests) == 2
    assert "Output ONLY the JSON object" in requests[1]["messages"][0]["content"]


def test_invalid_json_twice_raises(config, ollama):
    answers, _ = ollama
    answers += ["not json", '{"unexpected": "keys"}']
    with pytest.raises(LocalModelError, match="invalid JSON twice"):
        LocalDocumentModel("m").extract(side_by_side_doc(), config.fields)


def test_request_is_local_strict_and_never_asks_for_a_decision(config, ollama):
    answers, requests = ollama
    answers.append(json.dumps({f.name: None for f in config.fields}))
    LocalDocumentModel("qwen2.5vl:7b").extract(side_by_side_doc(), config.fields)
    body = requests[0]
    assert body["model"] == "qwen2.5vl:7b" and body["options"]["temperature"] == 0 and body["stream"] is False
    assert set(body["format"]["properties"]) == {f.name for f in config.fields}
    assert body["messages"][0]["images"]  # page rendered to an image locally
    prompt = body["messages"][0]["content"].lower()
    assert "match" not in prompt and "pass" not in prompt and "fail" not in prompt
