import sys
from pathlib import Path

import pymupdf
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdf_parser import load_fields  # noqa: E402


def make_pdf(*items: tuple[str, float, float]) -> bytes:
    """One-page PDF; each item is (text, x, y) with y = baseline distance from the top."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 40), "VRV GLOBAL TRADING TEST DOCUMENT", fontsize=12)  # enough text to not look scanned
    for text, x, y in items:
        page.insert_text((x, y), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def side_by_side_doc(invoice="INV-1024", po="PO-7781", qty="20", amount="USD 12,500.00") -> bytes:
    return make_pdf(
        ("Invoice Number:", 50, 100), (invoice, 220, 100),
        ("PO Number:", 50, 120), (po, 220, 120),
        ("Quantity", 50, 140), (qty, 220, 140),
        ("Currency", 50, 160), ("USD", 220, 160),
        ("Total Amount", 50, 180), (amount, 220, 180),
    )  # fmt: skip


def stacked_doc(invoice="INV-1024", po="PO-7781", qty="20", amount="USD 12,500.00") -> bytes:
    """Same data, different layout: values under labels, alternative labels, a heading."""
    return make_pdf(
        ("PURCHASE ORDER CONFIRMATION", 50, 70),
        ("Purchase Order Number", 50, 110), ("Invoice No", 250, 110),
        (po, 50, 126), (invoice, 250, 126),
        ("Qty", 50, 170), ("Amount", 200, 170),
        (qty, 52, 186), (amount, 200, 186),
    )  # fmt: skip


@pytest.fixture(scope="session")
def config():
    return load_fields(yaml.safe_load((ROOT / "config.yaml").read_text()))
