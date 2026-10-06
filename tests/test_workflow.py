from conftest import make_pdf, side_by_side_doc, stacked_doc

import main
from compare import format_reply, validate_pdfs
from storage import Storage


def test_matching_documents(config):
    result = validate_pdfs([("invoice.pdf", side_by_side_doc()), ("po.pdf", stacked_doc())], config)
    assert result["status"] == "PASS" and result["mismatches"] == [] and result["issues"] == []
    assert format_reply(result) == "VRV TradePilot validation completed.\n\nAll configured checks passed.\n\nStatus: GOOD TO GO"


def test_mismatching_documents(config):
    other = stacked_doc(qty="25", amount="USD 12,900.00")
    result = validate_pdfs([("invoice.pdf", side_by_side_doc()), ("po.pdf", other)], config)
    assert result["status"] == "FAIL"
    assert [(m["field"], m["document_1"], m["document_2"]) for m in result["mismatches"]] == [
        ("quantity", 20, 25),
        ("amount", "USD 12500.00", "USD 12900.00"),
    ]
    reply = format_reply(result)
    assert reply.startswith("VRV TradePilot found the following differences:\n\nQuantity\nDocument 1: 20\nDocument 2: 25")
    assert reply.endswith("Status: REVIEW REQUIRED")


def test_required_field_missing(config):
    no_po = make_pdf(("Invoice Number", 50, 100), ("INV-1024", 220, 100), ("Quantity", 50, 120), ("20", 220, 120),
                     ("Total Amount", 50, 140), ("USD 12,500.00", 220, 140))  # fmt: skip
    result = validate_pdfs([("document_1.pdf", side_by_side_doc()), ("document_2.pdf", no_po)], config)
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["issues"] == ['Required field "PO Number" could not be identified in document_2.pdf.']
    assert "Reason:\nRequired field \"PO Number\"" in format_reply(result)


def test_fewer_than_two_pdfs_and_unreadable_pdf(config):
    assert validate_pdfs([("a.pdf", side_by_side_doc())], config)["status"] == "REVIEW_REQUIRED"
    result = validate_pdfs([("a.pdf", side_by_side_doc()), ("b.pdf", b"%PDF-1.4 damaged")], config)
    assert result["status"] == "REVIEW_REQUIRED" and result["issues"][0].startswith("b.pdf:")


def _fake_outlook(monkeypatch, attachments):
    replies = []
    pdfs = {"a1": side_by_side_doc(), "a2": stacked_doc()}
    monkeypatch.setattr(main.outlook, "get_attachments", lambda mid: attachments)
    monkeypatch.setattr(main.outlook, "download_attachment", lambda mid, aid: pdfs[aid])
    monkeypatch.setattr(main.outlook, "reply_to_message", lambda mid, body: replies.append((mid, body)))
    monkeypatch.setattr(main.outlook, "mark_as_read", lambda mid: None)
    return replies


PDF_ATTACHMENTS = [
    {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "invoice.pdf", "contentType": "application/pdf", "size": 900},
    {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a2", "name": "po.pdf", "contentType": "application/pdf", "size": 900},
]  # fmt: skip


def test_duplicate_message_protection(tmp_path, config, monkeypatch):
    storage = Storage(str(tmp_path / "test.db"))
    replies = _fake_outlook(monkeypatch, PDF_ATTACHMENTS)
    message = {"id": "msg-1", "subject": "Docs", "hasAttachments": True}

    main.process_message(message, config, storage)
    main.process_message(message, config, storage)  # seen again on the next poll

    assert len(replies) == 1 and replies[0][0] == "msg-1"
    assert storage.get("msg-1")["status"] == "PASS"
    assert not storage.should_process("msg-1")


def test_email_without_pdfs_gets_no_reply(tmp_path, config, monkeypatch):
    """TradePilot's own reply lands in the same inbox; it must not trigger another reply."""
    storage = Storage(str(tmp_path / "test.db"))
    replies = _fake_outlook(monkeypatch, [])
    main.process_message({"id": "msg-2", "subject": "RE: Docs", "hasAttachments": False}, config, storage)
    assert replies == [] and storage.get("msg-2")["status"] == "IGNORED"


def test_outlook_failure_is_retried_but_never_double_replies(tmp_path, config, monkeypatch):
    storage = Storage(str(tmp_path / "test.db"))
    _fake_outlook(monkeypatch, PDF_ATTACHMENTS)

    def broken(mid, aid):
        raise main.outlook.OutlookError("Graph returned 503")

    monkeypatch.setattr(main.outlook, "download_attachment", broken)
    message = {"id": "msg-3", "hasAttachments": True}
    main.process_message(message, config, storage)
    assert storage.get("msg-3")["status"] == "FAILED" and storage.should_process("msg-3")

    replies = _fake_outlook(monkeypatch, PDF_ATTACHMENTS)  # Outlook works again on the next poll
    main.process_message(message, config, storage)
    main.process_message(message, config, storage)
    assert storage.get("msg-3")["status"] == "PASS" and len(replies) == 1


def test_start_time_is_remembered(tmp_path):
    db = str(tmp_path / "test.db")
    first = Storage(db).start_time()
    assert Storage(db).start_time() == first
