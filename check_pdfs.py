"""Check two PDFs locally, without email: shows each field, where it came from, and the reply.

    python check_pdfs.py invoice.pdf difficult.pdf

Uses the same settings as main.py (.env and config.yaml), including the optional local
model fallback. Nothing is sent anywhere and nothing is written to tradepilot.db.
"""

import json
import logging
import os
import sys
from pathlib import Path

import yaml

from compare import format_reply, validate_pdfs
from main import load_env, setup_local_model
from pdf_parser import load_fields


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    load_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(levelname)-7s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_fields(yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")))
    model = setup_local_model(config)
    pdfs = [(Path(p).name, Path(p).read_bytes()) for p in sys.argv[1:]]
    result = validate_pdfs(pdfs, config, model)

    print("\n--- fields ---")
    for doc in result.get("documents", []):
        print(doc["file"])
        for name, f in doc["fields"].items():
            print(f"  {name:<16} {f['status']:<10} {str(f['raw']):<22} source={f['source']}")
    print("\n--- result ---")
    print(json.dumps({k: result[k] for k in ("status", "mismatches", "issues")}, indent=2, default=str))
    print("\n--- reply that would be sent ---")
    print(format_reply(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
