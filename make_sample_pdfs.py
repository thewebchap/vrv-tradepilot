"""Create sample PDFs for the first test email:

    python make_sample_pdfs.py

samples/invoice.pdf + samples/po_match.pdf     -> TradePilot replies GOOD TO GO
samples/invoice.pdf + samples/po_mismatch.pdf  -> TradePilot lists the Quantity and Amount differences
"""

from pathlib import Path

import pymupdf


def write_pdf(path: Path, title: str, rows: list[tuple[str, str]], stacked: bool) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 60), title, fontsize=15)
    for i, (label, value) in enumerate(rows):
        if stacked:  # value under its label, in two columns
            x, y = 50 + (i % 2) * 250, 120 + (i // 2) * 50
            page.insert_text((x, y), label, fontsize=11)
            page.insert_text((x, y + 16), value, fontsize=11)
        else:  # value to the right of its label
            page.insert_text((50, 120 + i * 22), label, fontsize=11)
            page.insert_text((220, 120 + i * 22), value, fontsize=11)
    doc.save(path)
    doc.close()


def main() -> None:
    out = Path("samples")
    out.mkdir(exist_ok=True)
    common = [("Invoice Number:", "INV-1024"), ("PO Number:", "PO-7781"), ("Currency", "USD")]
    write_pdf(out / "invoice.pdf", "COMMERCIAL INVOICE", [*common, ("Quantity", "20"), ("Total Amount", "USD 12,500.00")], stacked=False)
    write_pdf(out / "po_match.pdf", "PURCHASE ORDER", [*common, ("Qty", "20"), ("Amount", "12,500.00")], stacked=True)
    write_pdf(out / "po_mismatch.pdf", "PURCHASE ORDER", [*common, ("Qty", "25"), ("Amount", "12,900.00")], stacked=True)
    for path in sorted(out.glob("*.pdf")):
        print(path)


if __name__ == "__main__":
    main()
