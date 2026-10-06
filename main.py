"""VRV TradePilot Version 1.

    python main.py

Signs in to Microsoft 365 as you (delegated OAuth, device code on first run), then every POLL_INTERVAL_SECONDS checks the Inbox for new emails,
compares the two PDF attachments of each, and replies in the same thread.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import yaml

import outlook
from compare import format_reply, review, validate_pdfs
from local_model import LocalDocumentModel, LocalModelError
from pdf_parser import FieldsConfig, load_fields
from storage import Storage

log = logging.getLogger("tradepilot")

MAX_PDF_BYTES = 25 * 1024 * 1024


def load_env(path: str = ".env") -> None:
    """Read KEY=VALUE lines from .env into the environment (real env vars take precedence)."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def is_pdf(attachment: dict) -> bool:
    return attachment.get("@odata.type") == "#microsoft.graph.fileAttachment" and (
        attachment.get("contentType") == "application/pdf" or (attachment.get("name") or "").lower().endswith(".pdf")
    )


def process_message(
    message: dict, config: FieldsConfig, storage: Storage, model: LocalDocumentModel | None = None
) -> None:
    message_id = message["id"]
    if not storage.should_process(message_id):
        return
    log.info("Processing %r from %s", message.get("subject"), (message.get("from") or {}).get("emailAddress", {}).get("address"))
    storage.save(message_id, "PROCESSING")
    try:
        attachments = outlook.get_attachments(message_id) if message.get("hasAttachments") else []
        pdf_attachments = [a for a in attachments if is_pdf(a)]
        log.info("Attachments: %s", [a.get("name") for a in attachments])

        if not pdf_attachments:
            # No PDFs (e.g. a plain reply in the thread, or TradePilot's own reply): nothing to
            # validate, and replying here could start an endless reply loop.
            storage.save(message_id, "IGNORED", {"reason": "no PDF attachments"})
            log.info("No PDF attachments; ignoring")
            return

        first_two = pdf_attachments[:2]  # Version 1 compares the first two PDFs only
        too_large = [a["name"] for a in first_two if (a.get("size") or 0) > MAX_PDF_BYTES]
        if too_large:
            result = review(f"{too_large[0]} is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.")
        else:
            pdfs = [(a["name"], outlook.download_attachment(message_id, a["id"])) for a in first_two]
            result = validate_pdfs(pdfs, config, model)
        if len(pdf_attachments) > 2:
            result["note"] = f"{len(pdf_attachments)} PDFs attached; only the first two were compared."
    except outlook.OutlookError as exc:
        log.error("Outlook error, will retry on a later poll: %s", exc)
        storage.save(message_id, "FAILED", error=str(exc))
        return
    except Exception as exc:
        log.exception("Unexpected error while processing the documents")
        result = review("An unexpected error occurred while processing the documents.")
        result["error"] = f"{type(exc).__name__}: {exc}"

    log.info("Result: %s  mismatches=%s  issues=%s", result["status"], [m["field"] for m in result["mismatches"]], result["issues"])
    storage.save(message_id, "REPLYING", result)  # from here on, never retried => never two replies
    try:
        outlook.reply_to_message(message_id, format_reply(result))
        log.info("Reply sent")
        storage.save(message_id, result["status"], result)
    except Exception as exc:
        log.error("Reply failed: %s", exc)
        storage.save(message_id, result["status"], result, error=f"reply failed: {exc}")
        return
    try:
        outlook.mark_as_read(message_id)
    except Exception as exc:  # cosmetic; the message is already recorded as processed
        log.warning("Could not mark message as read: %s", exc)


def poll_once(config: FieldsConfig, storage: Storage, since: str, model: LocalDocumentModel | None = None) -> None:
    log.info("Checking inbox")
    messages = [m for m in outlook.list_recent_messages(since) if storage.should_process(m["id"])]
    log.info("%d new message(s)", len(messages))
    for message in messages:
        process_message(message, config, storage, model)


def setup_local_model(config: FieldsConfig) -> LocalDocumentModel | None:
    """The optional local-model fallback, or None. Never stops TradePilot from starting."""
    try:
        model = LocalDocumentModel.from_env()
    except LocalModelError as exc:
        log.error("Local model fallback is OFF: %s", exc)
        return None
    if model is None or not config.use_local_model_fallback:
        log.info("Local model fallback: off (PDF parser only)")
        return None
    try:
        model.check()
        log.info("Local model fallback: on (ollama, %s)", model.model)
    except LocalModelError as exc:
        log.warning("Local model fallback: on, but %s. Documents that need it will get REVIEW REQUIRED.", exc)
    return model


def main() -> None:
    load_env()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)-7s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs are not useful at INFO

    config_data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    config = load_fields(config_data)
    interval = int(os.environ.get("POLL_INTERVAL_SECONDS") or config_data.get("poll_interval_seconds", 30))
    storage = Storage(os.environ.get("SQLITE_PATH", "tradepilot.db"))

    log.info("TradePilot started (polling every %ss)", interval)
    try:
        outlook.get_access_token()
    except outlook.OutlookError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None
    log.info("Authenticated as %s", outlook.signed_in_user())
    model = setup_local_model(config)
    since = storage.start_time()
    log.info("Processing emails received after %s", since)

    while True:
        try:
            poll_once(config, storage, since, model)
        except Exception as exc:  # never stop polling because of one failure
            log.error("Polling failed: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("TradePilot stopped")
