from decimal import Decimal

import pymupdf
import pytest
from conftest import make_pdf, side_by_side_doc, stacked_doc

from pdf_parser import (
    AMBIGUOUS,
    FOUND,
    NOT_FOUND,
    DocumentError,
    extract_fields,
    read_words,
    to_amount,
    to_date,
    to_integer,
)


def extract(pdf, config):
    return extract_fields(read_words(pdf), config)


# --- extraction ---------------------------------------------------------------------------


def test_quantity_on_same_row(config):
    result = extract(make_pdf(("Quantity", 50, 100), ("20", 220, 100)), config)["quantity"]
    assert (result.status, result.value) == (FOUND, 20)


def test_quantity_with_value_below(config):
    assert extract(make_pdf(("Quantity", 50, 100), ("20", 50, 116)), config)["quantity"].value == 20


def test_quantity_below_after_blank_line_indented(config):
    assert extract(make_pdf(("Quantity", 50, 100), ("20", 80, 132)), config)["quantity"].value == 20


def test_value_below_and_to_the_right(config):
    assert extract(make_pdf(("Quantity", 50, 100), ("20", 160, 116)), config)["quantity"].value == 20


def test_qty_alias(config):
    assert extract(make_pdf(("Qty", 50, 100), ("1,000", 220, 100)), config)["quantity"].value == 1000


def test_multi_word_label(config):
    pdf = make_pdf(("Purchase Order Number", 50, 100), ("PO-55120", 260, 100))
    assert [w.text for w in read_words(pdf)][-4:-1] == ["Purchase", "Order", "Number"]  # separate words
    assert extract(pdf, config)["po_number"].value == "PO-55120"


def test_hash_label_from_yaml(config):
    assert extract(make_pdf(("Invoice #", 50, 100), ("INV-77", 220, 100)), config)["invoice_number"].value == "INV-77"


def test_both_layouts_give_same_values(config):
    for pdf in (side_by_side_doc(), stacked_doc()):
        fields = extract(pdf, config)
        assert fields["invoice_number"].value == "INV-1024"
        assert fields["po_number"].value == "PO-7781"  # not the "CONFIRMATION" heading
        assert fields["quantity"].value == 20
        assert fields["amount"].value.value == Decimal("12500.00")


def test_value_not_taken_from_beyond_another_label(config):
    fields = extract(make_pdf(("Quantity", 50, 100), ("Currency", 200, 100), ("USD", 300, 100)), config)
    assert fields["quantity"].status == NOT_FOUND and fields["currency"].value == "USD"


def test_conflicting_values_are_ambiguous(config):
    pdf = make_pdf(("Quantity", 50, 100), ("20", 220, 100), ("Quantity", 50, 400), ("25", 220, 400))
    assert extract(pdf, config)["quantity"].status == AMBIGUOUS


def test_unusable_pdfs():
    blank = pymupdf.open()
    blank.new_page()
    with pytest.raises(DocumentError, match="no extractable text"):
        read_words(blank.tobytes())
    with pytest.raises(DocumentError, match="not a PDF"):
        read_words(b"hello")


# --- normalization -----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["1,000", "1000", "1 000"])
def test_integer_normalization(text):
    assert to_integer(text) == 1000


@pytest.mark.parametrize("text", ["12500", "12,500", "12,500.00"])
def test_decimal_normalization(text):
    assert to_amount(text, {}).value == Decimal("12500")


@pytest.mark.parametrize("text", ["USD 12,500", "$12,500", "12,500 USD"])
def test_currency_amounts(text):
    amount = to_amount(text, {"$": "USD"})
    assert (amount.value, amount.currency) == (Decimal("12500"), "USD")


def test_dates_are_never_guessed():
    with pytest.raises(ValueError, match="ambiguous"):
        to_date("03/04/2026", ["%d/%m/%Y", "%m/%d/%Y"])
    assert str(to_date("3 April 2026", ["%d %B %Y"])) == "2026-04-03"
