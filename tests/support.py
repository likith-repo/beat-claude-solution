"""A synthetic 20-row leads file built at import time, so no CSV is committed to the repo."""
import atexit
import csv
import shutil
import tempfile
from pathlib import Path

from triage.ingest import REQUIRED_COLUMNS, sha256_file

N_ROWS = 20
_BUDGETS = ["25000", "0", "500", "12000", "15k", "we'll discuss", "40000", "3000", "999999", "6000"]
_MESSAGES = ["We need paid media help.", "Looking for a retainer.", "", "Please send a proposal.", "Exploring options."]


def _rows():
    for i in range(1, N_ROWS + 1):
        has_site = i % 7 != 0
        yield {
            "lead_id": f"T-{i:03d}",
            "submitted_at": f"2026-06-{(i % 28) + 1:02d}T09:00:00Z",
            "name": f"Test Person {i}",
            "email": "" if i == 4 else f"person{i}@company{i}.example",
            "company": f"Company {i}",
            "website": f"https://company{i}.example" if has_site else "",
            "monthly_budget_usd": _BUDGETS[i % len(_BUDGETS)],
            "message": _MESSAGES[i % len(_MESSAGES)],
            "source": "contact_form" if i % 2 else "webinar",
        }


_dir = Path(tempfile.mkdtemp(prefix="triage-sample-"))
atexit.register(shutil.rmtree, _dir, ignore_errors=True)

FIXTURE = _dir / "sample_leads.csv"
with FIXTURE.open("w", newline="", encoding="utf-8") as _fh:
    _w = csv.DictWriter(_fh, fieldnames=REQUIRED_COLUMNS, lineterminator="\n")
    _w.writeheader()
    _w.writerows(_rows())

SAMPLE_SHA = sha256_file(FIXTURE)
